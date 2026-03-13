#!/usr/bin/env python3
"""Build a lightweight operator read-only index from canonical OpenClaw logs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
import re

TASK_REGISTRY = Path("/Users/AGENT/.openclaw/workspace/logs/tasks/tasks.ndjson")
DAILY_LEDGER_DIR = Path("/Users/AGENT/.openclaw/workspace/logs/daily")
OCTR_BUNDLES_DIR = Path("/Users/AGENT/octriage-bundles")
RADCHECK_SCORE_PATH = Path("/Users/AGENT/.openclaw/watchdog/reliability_score.json")
INCIDENTS_DIR = Path("/Users/AGENT/.openclaw/workspace/logs/incidents")
DECISIONS_DIR = Path("/Users/AGENT/.openclaw/workspace/logs/decisions")
INDEX_DIR = Path("/Users/AGENT/.openclaw/workspace/logs/index")

INDEX_OUTPUT = INDEX_DIR / "operator_log_index.json"
TIMELINE_OUTPUT = INDEX_DIR / "operator_log_timeline.md"
SUMMARY_OUTPUT = INDEX_DIR / "operator_status_summary.txt"


def parse_timestamp(raw: Any, *, fallback: Optional[float] = None) -> str:
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception:
            pass
    if isinstance(raw, str):
        parsed = raw.strip()
        if parsed:
            return parsed
    if fallback is None:
        return ""
    return datetime.fromtimestamp(fallback, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                records.append(parsed)
        except json.JSONDecodeError:
            continue
    return records


def file_modified_iso(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
    except Exception:
        return ""


def safe_float(path: Path, value: Any) -> Optional[float]:
    try:
        return float(value)
    except Exception:
        try:
            return path.stat().st_mtime
        except Exception:
            return None


@dataclass
class TimedEvent:
    timestamp: str
    source: str
    kind: str
    event: str
    details: Dict[str, Any]
    sort_key: str

    def markdown_line(self, date_prefix: bool = False) -> str:
        ts = self.timestamp or self.sort_key
        if date_prefix and self.timestamp:
            return f"- {self.timestamp} [{self.source}] {self.event}"
        if self.timestamp:
            if "T" in self.timestamp and len(self.timestamp) >= 16:
                return f"- {self.timestamp[11:16]} - {self.event}"
            return f"- {self.timestamp} - {self.event}"
        return f"- {self.kind}: {self.event}"


def safe_status(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    return ""


def parse_task_registry() -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "events": [],
        "active_tasks": 0,
        "completed_tasks_today": 0,
        "blocked_tasks": 0,
        "latest_proof_artifacts": [],
        "task_status_by_id": {},
        "source_status": "present",
        "errors": [],
    }

    if not TASK_REGISTRY.exists():
        result["source_status"] = "missing"
        return result

    entries = parse_jsonl(TASK_REGISTRY)
    if not entries:
        result["source_status"] = "present"

    status_by_task: Dict[str, str] = {}
    completion_today_date: str = datetime.utcnow().date().isoformat()

    for entry in entries:
        task_id = entry.get("task_id") or "unknown"
        status = safe_status(entry.get("status"))
        event_type = entry.get("event_type")
        if isinstance(event_type, str):
            event_type = event_type.strip()
        if not event_type:
            if status in {"completed", "failed"}:
                event_type = "task_completed" if status == "completed" else "task_failed"
            elif status in {"blocked", "blocked_waiting"}:
                event_type = "task_blocked"
            elif status in {"active", "in_progress"}:
                event_type = "task_updated"
            elif status == "created":
                event_type = "task_created"
            else:
                event_type = "task_updated"
        ts = parse_timestamp(entry.get("timestamp"))
        if not ts:
            ts = file_modified_iso(TASK_REGISTRY)

        if ts:
            result["events"].append(
                {
                    "timestamp": ts,
                    "kind": event_type,
                    "task_id": task_id,
                    "path": str(TASK_REGISTRY),
                    "proof": entry.get("evidence_path"),
                    "status": entry.get("status"),
                    "repo": entry.get("repo"),
                    "owner": entry.get("owner"),
                    "objective": entry.get("objective"),
                }
            )

        if event_type == "proof_attached" and entry.get("evidence_path"):
            result["latest_proof_artifacts"].append(
                {
                    "task_id": task_id,
                    "evidence_path": entry.get("evidence_path"),
                    "timestamp": ts,
                    "path": str(TASK_REGISTRY),
                }
            )

        if status:
            status_by_task[task_id] = status

        if event_type in ("task_completed", "task_created", "task_updated", "task_blocked", "proof_attached", "task_failed"):
            status_by_task.setdefault(task_id, safe_status(entry.get("status")) or safe_status(event_type.replace("task_", "")))

    # derive active/completed/block from latest known status by task
    # fallback: count create/completed events if no status is explicit
    creates = [e for e in result["events"] if e["kind"] == "task_created"]
    completed = [e for e in result["events"] if e["kind"] in ("task_completed", "task_failed")]
    blocked = [e for e in result["events"] if e["kind"] == "task_blocked"]

    active_tasks = 0
    blocked_tasks = 0
    for t_status in status_by_task.values():
        if t_status in {"completed", "resolved", "failed", "done"}:
            continue
        if t_status in {"blocked", "blocked_waiting", "blocked_by_dependency", "blocked_by_input"}:
            blocked_tasks += 1
            continue
        if t_status:
            active_tasks += 1

    if active_tasks == 0:
        active_tasks = max(0, len(creates) - len(completed) - len(blocked))

    completed_today = [
        e
        for e in completed
        if e.get("timestamp", "").startswith(completion_today_date)
    ]

    result.update(
        {
            "active_tasks": max(0, active_tasks),
            "completed_tasks_today": len(completed_today),
            "blocked_tasks": blocked_tasks,
        }
    )
    return result


def parse_daily_ledger() -> Dict[str, Any]:
    output: Dict[str, Any] = {
        "current_date": datetime.utcnow().date().isoformat(),
        "entries": [],
        "source_status": "missing",
        "filename": None,
        "snippet": {},
    }

    if not DAILY_LEDGER_DIR.exists():
        output["source_status"] = "missing"
        return output

    candidates = sorted(DAILY_LEDGER_DIR.glob("*.md"))
    if not candidates:
        output["source_status"] = "parse_error"
        return output

    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    output["filename"] = str(latest)
    output["source_status"] = "present"

    text = latest.read_text(encoding="utf-8", errors="ignore")
    section_map: Dict[str, str] = {}
    current = None
    for line in text.splitlines():
        heading = re.match(r"^(?:#{1,3}\s*)?([A-Z][A-Z\s/]+)$", line.strip())
        if heading:
            current = heading.group(1).strip()
            section_map[current] = ""
            continue
        if current:
            section_map[current] = section_map[current] + (line + "\n")

    # best-effort extraction
    for section in ("SYSTEM STATE", "ACTIVE TASKS", "COMPLETED TASKS", "INCIDETS / ANOMALIES", "DECISIONS", "INCIDENTS / ANOMALIES"):
        if section in section_map:
            output["snippet"][section] = section_map[section].strip()

    # if date line exists use it for current_date
    m = re.search(r"^DATE:\s*(\d{4}-\d{2}-\d{2})", text, re.MULTILINE)
    if m:
        output["current_date"] = m.group(1)

    output["entries"].append(
        {
            "path": str(latest),
            "timestamp": parse_timestamp(None, fallback=latest.stat().st_mtime),
            "summary": " | ".join(
                [
                    line.strip()
                    for line in text.splitlines()
                    if line.strip() and "ACTIVE TASKS" not in line and "DATE:" not in line
                ][:6]
            ),
        }
    )

    return output


def parse_latest_file_in_dir(base: Path) -> Optional[Dict[str, str]]:
    if not base.exists():
        return None
    items = [p for p in base.iterdir() if p.is_file()]
    if not items:
        return None
    latest = max(items, key=lambda p: p.stat().st_mtime)
    return {
        "path": str(latest),
        "name": latest.name,
        "timestamp": parse_timestamp(None, fallback=latest.stat().st_mtime),
    }


def parse_octriage_bundles() -> Dict[str, Any]:
    out: Dict[str, Any] = {"source_status": "missing", "latest": None}
    if not OCTR_BUNDLES_DIR.exists():
        return out

    candidates = [
        p
        for p in OCTR_BUNDLES_DIR.iterdir()
        if p.is_dir() and re.match(r"^\d{8}-\d{6}$", p.name)
    ]
    if not candidates:
        out["source_status"] = "parse_error"
        return out

    latest = max(candidates, key=lambda p: p.name)
    summary = latest / "bundle_summary.txt"
    out.update(
        {
            "source_status": "present",
            "latest": {
                "bundle_id": latest.name,
                "bundle_path": str(latest),
                "bundle_summary_path": str(summary) if summary.exists() else None,
                "created": parse_timestamp(None, fallback=latest.stat().st_mtime),
            },
        }
    )
    return out


def parse_radcheck() -> Dict[str, Any]:
    out: Dict[str, Any] = {"source_status": "missing"}
    if not RADCHECK_SCORE_PATH.exists():
        return out

    try:
        data = json.loads(RADCHECK_SCORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"source_status": "parse_error"}

    artifacts = []
    try:
        rel_artifacts = data.get("artifacts", {})
        for name, path in rel_artifacts.items():
            if isinstance(path, str):
                artifacts.append({"name": name, "path": path})
    except Exception:
        pass

    return {
        "source_status": "present",
        "score": data.get("score"),
        "system_state": data.get("risk_level") or data.get("system_state") or "unknown",
        "generated_at": data.get("generated_at"),
        "artifacts": artifacts,
        "radcheck_version": data.get("radcheck_version"),
    }


def parse_decision_or_incident_file(entry: Dict[str, str]) -> str:
    return f"{Path(entry['path']).name}"


def build_timeline(
    tasks: Dict[str, Any],
    daily: Dict[str, Any],
    octriage: Dict[str, Any],
    radcheck: Dict[str, Any],
    incident: Optional[Dict[str, str]],
    decision: Optional[Dict[str, str]],
    current_date: str,
) -> List[Dict[str, str]]:
    events: List[TimedEvent] = []

    for entry in tasks.get("events", []):
        event_type = entry.get("kind", "task")
        task_id = entry.get("task_id")
        if event_type in {"task_created", "task_completed", "task_blocked", "proof_attached", "task_failed"}:
            if event_type == "task_created":
                label = f"Task created: {task_id}"
            elif event_type == "task_completed":
                label = f"Task completed: {task_id}"
            elif event_type == "task_blocked":
                label = f"Task blocked: {task_id}"
            elif event_type == "proof_attached":
                label = f"Proof attached for task: {task_id}"
            else:
                label = f"Task failed: {task_id}"
            if entry.get("evidence_path"):
                label += f" ({entry['evidence_path']})"
            ts = entry.get("timestamp") or file_modified_iso(TASK_REGISTRY)
            events.append(
                TimedEvent(
                    timestamp=ts,
                    source="tasks",
                    kind=event_type,
                    event=label,
                    details={
                        "task_id": task_id,
                        "path": entry.get("path"),
                        "status": entry.get("status"),
                    },
                    sort_key=ts,
                )
            )

    if octriage.get("latest"):
        b = octriage["latest"]
        events.append(
            TimedEvent(
                timestamp=b.get("created") or "",
                source="octriage",
                kind="octriage_bundle_generated",
                event=f"OCTriage proof bundle generated: {b.get('bundle_id')}",
                details={"bundle_path": b.get("bundle_path"), "summary": b.get("bundle_summary_path")},
                sort_key=b.get("created") or "",
            )
        )

    if radcheck.get("source_status") == "present":
        events.append(
            TimedEvent(
                timestamp=radcheck.get("generated_at") or parse_timestamp(None, fallback=RADCHECK_SCORE_PATH.stat().st_mtime),
                source="radcheck",
                kind="radcheck_updated",
                event=f"RadCheck score updated: {radcheck.get('score')}",
                details={"artifacts": radcheck.get("artifacts")},
                sort_key=radcheck.get("generated_at") or parse_timestamp(None, fallback=RADCHECK_SCORE_PATH.stat().st_mtime),
            )
        )

    if incident:
        events.append(
            TimedEvent(
                timestamp=incident.get("timestamp") or "",
                source="incidents",
                kind="incident_logged",
                event=f"Incident logged: {incident.get('name')}",
                details=incident,
                sort_key=incident.get("timestamp") or "",
            )
        )

    if decision:
        events.append(
            TimedEvent(
                timestamp=decision.get("timestamp") or "",
                source="decisions",
                kind="decision_logged",
                event=f"Decision log: {decision.get('name')}",
                details=decision,
                sort_key=decision.get("timestamp") or "",
            )
        )

    # sort descending
    events.sort(key=lambda e: e.sort_key, reverse=True)

    # prefer same-day events for timeline output and keep compact
    date_prefix = current_date or datetime.utcnow().date().isoformat()
    timeline_lines = [
        e.markdown_line(date_prefix=(not e.timestamp.startswith(date_prefix)) if e.timestamp else False)
        for e in events
    ]

    return [
        {
            "timestamp": e.timestamp,
            "source": e.source,
            "kind": e.kind,
            "event": e.event,
            "details": e.details,
        }
        for e in events
    ], timeline_lines


def build_summary_lines(index: Dict[str, Any], current_date: str) -> List[str]:
    return [
        "OpenClaw Operator Summary\n",
        f"Generated At: {index.get('generated_at', 'unknown')}",
        f"Current OpenClaw Reliability Score: {index.get('current_openclaw_reliability_score', 'unknown')}",
        f"Current System State: {index.get('current_system_state', 'unknown')}",
        f"Active Tasks: {index.get('active_tasks', 0)}",
        f"Blocked Tasks: {index.get('blocked_tasks', 0)}",
        f"Latest OCTriage Bundle: {index.get('latest_octriage_bundle') or 'none'}",
        f"Latest Incident: {index.get('latest_incidents') or 'none'}",
        f"Latest Decision Log: {index.get('latest_decisions') or 'none'}",
        f"Date: {current_date}",
        "",
    ]


def main() -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    task_data = parse_task_registry()
    daily_data = parse_daily_ledger()
    oct_data = parse_octriage_bundles()
    rad_data = parse_radcheck()
    latest_incident = parse_latest_file_in_dir(INCIDENTS_DIR)
    latest_decision = parse_latest_file_in_dir(DECISIONS_DIR)

    current_date = daily_data.get("current_date") or date.today().isoformat()

    timeline_events, timeline_lines = build_timeline(
        task_data,
        daily_data,
        oct_data,
        rad_data,
        latest_incident,
        latest_decision,
        current_date,
    )

    source_status = {
        "task_registry": task_data.get("source_status", "missing"),
        "daily_ledgers": daily_data.get("source_status", "missing"),
        "octriage_bundles": oct_data.get("source_status", "missing"),
        "radcheck": rad_data.get("source_status", "missing"),
        "incidents": "present" if INCIDENTS_DIR.exists() else "missing",
        "decisions": "present" if DECISIONS_DIR.exists() else "missing",
    }

    task_events = task_data.get("events", [])
    proof_events = [e for e in task_events if e.get("kind") == "proof_attached"]
    incident_events = [latest_incident] if latest_incident else []
    decision_events = [latest_decision] if latest_decision else []

    latest_proof = sorted(
        task_data.get("latest_proof_artifacts", []),
        key=lambda item: item.get("timestamp", ""),
        reverse=True,
    )
    latest_proof_path = latest_proof[0].get("evidence_path") if latest_proof else None

    latest_incident_display = latest_incident.get("name") if latest_incident else None
    latest_decision_display = latest_decision.get("name") if latest_decision else None

    index_payload: Dict[str, Any] = {
        "generated_at": datetime.utcnow().replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
        "current_date": current_date,
        "active_tasks": task_data.get("active_tasks", 0),
        "completed_tasks_today": task_data.get("completed_tasks_today", 0),
        "blocked_tasks": task_data.get("blocked_tasks", 0),
        "latest_proof_artifacts": latest_proof_path,
        "latest_incidents": latest_incident_display,
        "latest_decisions": latest_decision_display,
        "current_openclaw_reliability_score": rad_data.get("score", "unknown"),
        "current_system_state": rad_data.get("system_state", "unknown"),
        "latest_octriage_bundle": oct_data.get("latest", {}).get("bundle_id") if oct_data.get("latest") else None,
        "latest_radcheck_artifacts": rad_data.get("artifacts", []),
        "source_status": source_status,
        "timeline_events": timeline_events,
        "task_events": task_events,
        "proof_events": proof_events,
        "incident_events": incident_events,
        "decision_events": decision_events,
    }

    with INDEX_OUTPUT.open("w", encoding="utf-8") as f:
        json.dump(index_payload, f, indent=2)

    timeline_lines_rendered = [f"# Operator Log Timeline\n", f"Date: {current_date}\n", "TODAY\n"]
    timeline_lines_rendered.extend(timeline_lines[:40] if timeline_lines else ["- no recent operator events recorded"])
    TIMELINE_OUTPUT.write_text("\n".join(timeline_lines_rendered) + "\n", encoding="utf-8")

    summary = build_summary_lines(index_payload, current_date)
    SUMMARY_OUTPUT.write_text("\n".join(summary), encoding="utf-8")


if __name__ == "__main__":
    main()
