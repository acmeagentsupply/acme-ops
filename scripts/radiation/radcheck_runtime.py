#!/usr/bin/env python3
"""RadCheck runtime wrapper.

Preserves the existing scoring engine and scanner while providing a stable
operator-facing surface for CLI integration.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

RADCHECK_VERSION = "v2"
ENGINE_ID = "radcheck_scoring_v2"

HOME = Path.home()
OC_ROOT = HOME / ".openclaw"
WATCHDOG_DIR = OC_ROOT / "watchdog"
WORKSPACE_ROOT = OC_ROOT / "workspace"
RADCHECK_SOURCE_DIR = WORKSPACE_ROOT / "openclaw-ops" / "scripts" / "radiation"

RADIATION_CHECK_FILE = RADCHECK_SOURCE_DIR / "radiation_check.py"
ENGINE_FILE = RADCHECK_SOURCE_DIR / "radcheck_scoring_v2.py"

RELIABILITY_SCORE_FILE = WATCHDOG_DIR / "reliability_score.json"
RELIABILITY_SUMMARY_FILE = WATCHDOG_DIR / "reliability_summary.txt"
RADCHECK_HISTORY_FILE = WATCHDOG_DIR / "radcheck_history.ndjson"
RADIATION_FINDINGS_FILE = WATCHDOG_DIR / "radiation_findings.log"

EXIT_SUCCESS = 0
EXIT_RUNTIME_FAILURE = 1
EXIT_ARTIFACT_FAILURE = 2
EXIT_SELF_TEST_FAILURE = 3


@dataclass
class RuntimeResult:
    code: int
    payload: Optional[Dict[str, Any]] = None
    message: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.replace(path)


def _read_ndjson(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            s = line.strip()
            if not s:
                continue
            try:
                out.append(json.loads(s))
            except json.JSONDecodeError:
                continue
    return out


def _parse_json_stdout(text: str) -> Dict[str, Any]:
    raw = text.strip()
    if not raw:
        raise ValueError("No JSON output received from radiation_check")
    return json.loads(raw)


def _extract_domain_subscores(history_entry: Dict[str, Any]) -> Dict[str, int]:
    domains = history_entry.get("domains", {})
    out: Dict[str, int] = {}
    for name, data in domains.items():
        if isinstance(data, dict):
            out[name] = int(data.get("subscore", 0))
    return out


def _severity_rank(value: Any) -> int:
    ranks = {
        "CRITICAL": 0,
        "HIGH": 1,
        "MEDIUM": 2,
        "LOW": 3,
        "INFO": 4,
    }
    return ranks.get(str(value).upper(), 99)


def _humanize_identifier(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").strip().title()


def _extract_top_findings(scan_json: Dict[str, Any], limit: int = 2) -> List[str]:
    findings = scan_json.get("findings", [])
    if not isinstance(findings, list):
        return []

    ranked: List[Dict[str, Any]] = []
    for finding in findings:
        if isinstance(finding, dict):
            ranked.append(finding)

    ranked.sort(
        key=lambda finding: (
            _severity_rank(finding.get("severity")),
            str(finding.get("id", "")),
        )
    )

    top_findings: List[str] = []
    for finding in ranked:
        label = (
            finding.get("title")
            or finding.get("summary")
            or finding.get("message")
            or finding.get("kind")
            or finding.get("id")
            or finding.get("finding_id")
        )
        if not label:
            continue
        text = str(label).strip()
        if text in top_findings:
            continue
        if text == str(finding.get("id", "")).strip() or text == str(finding.get("finding_id", "")).strip():
            text = _humanize_identifier(text)
        top_findings.append(text)
        if len(top_findings) >= limit:
            break
    return top_findings


def _build_score_artifact(scan_json: Dict[str, Any], history_entry: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    score = int(scan_json.get("score", 0))
    risk_level = str(scan_json.get("risk", "UNKNOWN"))

    domain_subscores: Dict[str, int] = {}
    credits_total: Optional[int] = None
    velocity: Optional[Dict[str, Any]] = None

    if history_entry:
        domain_subscores = _extract_domain_subscores(history_entry)
        credits_total = history_entry.get("credits_total")
        velocity = history_entry.get("risk_velocity")
        risk_level = str(history_entry.get("risk_level", risk_level))

    metrics = scan_json.get("metrics", {})
    top_findings = _extract_top_findings(scan_json)

    return {
        "version": RADCHECK_VERSION,
        "radcheck_version": RADCHECK_VERSION,
        "engine": ENGINE_ID,
        "generated_at": _utc_now(),
        "score": score,
        "risk_level": risk_level,
        "domain_subscores": domain_subscores,
        "top_findings": top_findings,
        "credits": {
            "total": credits_total,
        },
        "resource_norm": scan_json.get("resource_norm"),
        "risk_velocity": velocity,
        "metrics": {
            "duration_ms": metrics.get("duration_ms"),
            "findings_count": metrics.get("findings_count"),
            "files_scanned": metrics.get("files_scanned"),
            "errors_encountered": metrics.get("errors_encountered"),
        },
        "artifacts": {
            "reliability_score": str(RELIABILITY_SCORE_FILE),
            "reliability_summary": str(RELIABILITY_SUMMARY_FILE),
            "history": str(RADCHECK_HISTORY_FILE),
        },
    }


def _format_default_output(score_obj: Dict[str, Any]) -> str:
    lines = [
        f"RadCheck {score_obj.get('radcheck_version', RADCHECK_VERSION)}",
        f"Reliability Score: {score_obj.get('score', 0)}",
        f"Risk Level: {score_obj.get('risk_level', 'UNKNOWN')}",
        "",
        "Domains",
    ]

    domains = score_obj.get("domain_subscores", {})
    if domains:
        for name in [
            "watchdog_health",
            "gateway_stability",
            "compaction_risk",
            "backup_posture",
            "resource_pressure",
        ]:
            if name in domains:
                lines.append(f"{name}: {domains[name]}")
    else:
        lines.append("(domain subscores unavailable)")

    lines += [
        "",
        "Artifacts written:",
        str(RELIABILITY_SCORE_FILE),
        str(RELIABILITY_SUMMARY_FILE),
        str(RADCHECK_HISTORY_FILE),
    ]
    return "\n".join(lines)


def _format_summary_output(score_obj: Dict[str, Any]) -> str:
    score = score_obj.get("score", 0)
    risk = score_obj.get("risk_level", "UNKNOWN")
    top_findings = score_obj.get("top_findings", [])
    if not isinstance(top_findings, list):
        top_findings = []

    lines = [
        f"RadCheck {score_obj.get('version', RADCHECK_VERSION)}",
        f"Score: {score}",
        f"Risk Level: {risk}",
        "",
        "Top findings",
    ]
    if top_findings:
        lines.extend(top_findings)
    else:
        lines.append("No major findings")
    return "\n".join(lines)


def _format_explain_output(score_obj: Dict[str, Any]) -> str:
    lines = [
        f"RadCheck {score_obj.get('radcheck_version', RADCHECK_VERSION)} Explain",
        f"Score: {score_obj.get('score', 0)}",
        f"Risk Level: {score_obj.get('risk_level', 'UNKNOWN')}",
        "",
        "Domain Subscores",
    ]

    domains = score_obj.get("domain_subscores", {})
    if domains:
        for name, value in domains.items():
            lines.append(f"- {name}: {value}")
    else:
        lines.append("- unavailable")

    credits = score_obj.get("credits", {})
    lines += [
        "",
        "Credits",
        f"- total: {credits.get('total')}",
        "",
        "Resource Normalization",
        f"- {score_obj.get('resource_norm')}",
    ]

    velocity = score_obj.get("risk_velocity")
    if velocity:
        lines += [
            "",
            "Risk Velocity",
            f"- delta: {velocity.get('delta')}",
            f"- direction: {velocity.get('direction')}",
            f"- rate_per_hour: {velocity.get('rate_per_hour')}",
        ]

    return "\n".join(lines)


def _write_summary_artifact(default_text: str, score_obj: Dict[str, Any]) -> None:
    summary_lines = [
        default_text,
        "",
        _format_summary_output(score_obj),
    ]
    RELIABILITY_SUMMARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    RELIABILITY_SUMMARY_FILE.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")


def _ensure_artifacts_exist() -> Optional[str]:
    for path in [RELIABILITY_SCORE_FILE, RELIABILITY_SUMMARY_FILE, RADCHECK_HISTORY_FILE]:
        if not path.exists():
            return f"Missing required artifact: {path}"
    return None


def run_full_scan_and_write_artifacts() -> RuntimeResult:
    if not RADIATION_CHECK_FILE.exists():
        return RuntimeResult(EXIT_RUNTIME_FAILURE, message=f"Missing scanner: {RADIATION_CHECK_FILE}")

    cmd = [sys.executable, str(RADIATION_CHECK_FILE), "--quiet", "--json"]
    proc = subprocess.run(
        cmd,
        cwd=str(WORKSPACE_ROOT / "openclaw-ops"),
        capture_output=True,
        text=True,
    )

    if proc.returncode != 0:
        err = proc.stderr.strip() or proc.stdout.strip() or f"radiation_check exit={proc.returncode}"
        return RuntimeResult(EXIT_RUNTIME_FAILURE, message=err)

    try:
        scan_json = _parse_json_stdout(proc.stdout)
    except Exception as exc:
        return RuntimeResult(EXIT_RUNTIME_FAILURE, message=f"Failed to parse scanner output: {exc}")

    history_entries = _read_ndjson(RADCHECK_HISTORY_FILE)
    latest = history_entries[-1] if history_entries else None

    score_obj = _build_score_artifact(scan_json, latest)
    _write_json(RELIABILITY_SCORE_FILE, score_obj)

    default_text = _format_default_output(score_obj)
    _write_summary_artifact(default_text, score_obj)

    return RuntimeResult(EXIT_SUCCESS, payload=score_obj)


def run_self_test() -> RuntimeResult:
    if not ENGINE_FILE.exists():
        return RuntimeResult(EXIT_SELF_TEST_FAILURE, message=f"Missing engine: {ENGINE_FILE}")

    before_score_mtime = RELIABILITY_SCORE_FILE.stat().st_mtime if RELIABILITY_SCORE_FILE.exists() else None
    before_summary_mtime = RELIABILITY_SUMMARY_FILE.stat().st_mtime if RELIABILITY_SUMMARY_FILE.exists() else None
    before_history_mtime = RADCHECK_HISTORY_FILE.stat().st_mtime if RADCHECK_HISTORY_FILE.exists() else None

    proc = subprocess.run(
        [sys.executable, str(ENGINE_FILE)],
        cwd=str(WORKSPACE_ROOT / "openclaw-ops"),
        capture_output=True,
        text=True,
    )

    if proc.returncode != 0:
        return RuntimeResult(EXIT_SELF_TEST_FAILURE, message=proc.stderr.strip() or proc.stdout.strip())

    if "Self-test PASS" not in proc.stdout:
        return RuntimeResult(EXIT_SELF_TEST_FAILURE, message=proc.stdout.strip())

    after_score_mtime = RELIABILITY_SCORE_FILE.stat().st_mtime if RELIABILITY_SCORE_FILE.exists() else None
    after_summary_mtime = RELIABILITY_SUMMARY_FILE.stat().st_mtime if RELIABILITY_SUMMARY_FILE.exists() else None
    after_history_mtime = RADCHECK_HISTORY_FILE.stat().st_mtime if RADCHECK_HISTORY_FILE.exists() else None

    changed = (
        before_score_mtime != after_score_mtime
        or before_summary_mtime != after_summary_mtime
        or before_history_mtime != after_history_mtime
    )

    lines = [
        "radcheck_scoring_v2 self-test: PASS",
        f"artifact_unchanged: {'no' if changed else 'yes'}",
    ]

    return RuntimeResult(EXIT_SUCCESS, payload={"stdout": proc.stdout, "artifact_changed": changed}, message="\n".join(lines))


def print_history(limit: int = 10) -> RuntimeResult:
    entries = _read_ndjson(RADCHECK_HISTORY_FILE)
    if not entries:
        return RuntimeResult(EXIT_ARTIFACT_FAILURE, message=f"No history available at {RADCHECK_HISTORY_FILE}")

    lines = ["RadCheck History"]
    for entry in entries[-limit:]:
        lines.append(
            f"{entry.get('ts', 'unknown')}  score={entry.get('score', 'n/a')}  risk={entry.get('risk_level', 'n/a')}"
        )

    return RuntimeResult(EXIT_SUCCESS, message="\n".join(lines))


def load_score_artifact() -> RuntimeResult:
    missing = _ensure_artifacts_exist()
    if missing:
        return RuntimeResult(EXIT_ARTIFACT_FAILURE, message=missing)

    try:
        return RuntimeResult(EXIT_SUCCESS, payload=_read_json(RELIABILITY_SCORE_FILE))
    except Exception as exc:
        return RuntimeResult(EXIT_ARTIFACT_FAILURE, message=f"Unable to read score artifact: {exc}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RadCheck runtime wrapper")
    parser.add_argument("--json", "-json", dest="json_mode", action="store_true", help="Print JSON output")
    parser.add_argument(
        "--summary", "-summary", dest="summary_mode", action="store_true", help="Print short summary output"
    )
    parser.add_argument("--history", "-history", dest="history_mode", action="store_true", help="Print history")
    parser.add_argument(
        "--self-test", "-self-test", dest="self_test_mode", action="store_true", help="Run engine self-test"
    )
    parser.add_argument(
        "--explain", "-explain", dest="explain_mode", action="store_true", help="Print score explanation"
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()

    if args.self_test_mode:
        result = run_self_test()
        if result.payload and result.payload.get("stdout"):
            print(result.payload["stdout"].rstrip())
        if result.message:
            print(result.message)
        return result.code

    if args.history_mode:
        result = print_history(limit=10)
        if result.message:
            print(result.message)
        return result.code

    # Default execution path: runtime runs full scanner + artifact generation.
    run_result = run_full_scan_and_write_artifacts()
    if run_result.code != EXIT_SUCCESS:
        print(run_result.message, file=sys.stderr)
        return run_result.code

    score_result = load_score_artifact()
    if score_result.code != EXIT_SUCCESS or not score_result.payload:
        print(score_result.message, file=sys.stderr)
        return score_result.code

    score_obj = score_result.payload

    if args.json_mode:
        print(json.dumps(score_obj, indent=2))
        return EXIT_SUCCESS

    if args.summary_mode:
        print(_format_summary_output(score_obj))
        return EXIT_SUCCESS

    if args.explain_mode:
        print(_format_explain_output(score_obj))
        return EXIT_SUCCESS

    print(_format_default_output(score_obj))
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
