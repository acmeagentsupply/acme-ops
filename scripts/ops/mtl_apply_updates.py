#!/usr/bin/env python3
"""
mtl_apply_updates.py — MTL + Dashboard Generator
==================================================
Reads append-only NDJSON delta log → produces deterministic MTL.md,
DASHBOARD.md, and MTL.snapshot.json.

Usage:
    python3 mtl_apply_updates.py [--repo-root <path>]

Exit code: always 0. Errors reported in DASHBOARD.md WARNINGS section.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ─── Paths ────────────────────────────────────────────────────────────────────
DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[3]  # workspace root

def _repo(root: Path) -> Dict[str, Path]:
    # root = workspace/, ops files live at workspace/openclaw-ops/ops/
    ops = root / "openclaw-ops" / "ops"
    return {
        "mtl_updates": ops / "mtl_updates.ndjson",
        "mtl_out":     ops / "MTL.md",
        "dashboard":   ops / "DASHBOARD.md",
        "snapshot":    ops / "MTL.snapshot.json",
    }

# ─── Constants ─────────────────────────────────────────────────────────────────
PRIORITY_ORDER = {"HIGH": 0, "MED": 1, "LOW": 2, "": 9}
STATUS_ORDER   = ["EXPECTED_PROOFS", "ACTIVE", "BLOCKED", "WATCH", "DONE"]
VALID_OPS      = {"ADD", "MOVE", "UPDATE", "PROOF_EXPECTED", "PROOF_RECEIVED", "COMMENT"}
VALID_STATUSES = {"ACTIVE", "BLOCKED", "WATCH", "DONE", "EXPECTED_PROOFS"}
DONE_WINDOW_DAYS = 14

# ─── Task state ───────────────────────────────────────────────────────────────
class Task:
    def __init__(self, task_id: str):
        self.task_id       = task_id
        self.status        = "ACTIVE"
        self.owner         = ""
        self.priority      = "MED"
        self.title         = ""
        self.proof_required = "NO"
        self.proof_items:  List[str] = []
        self.proof_status  = ""   # "EXPECTED" | "RECEIVED" | ""
        self.blocked_on    = ""   # "CHIP" | "HENDRIK" | "EXTERNAL"
        self.depends_on    = ""
        self.note          = ""
        self.done_ts       = ""   # ISO ts when moved to DONE
        self.last_updated  = ""

    def apply_event(self, ev: dict, warnings: List[str]) -> None:
        op = ev.get("op", "").upper()

        if op == "ADD":
            self.owner          = ev.get("owner", self.owner)
            self.priority       = ev.get("priority", self.priority) or self.priority
            self.title          = ev.get("title", self.title)
            self.proof_required = ev.get("proof_required", self.proof_required)
            self.depends_on     = ev.get("depends_on", self.depends_on)
            self.blocked_on     = ev.get("blocked_on", self.blocked_on)
            self.note           = ev.get("note", self.note)
            new_status = ev.get("status_to", "")
            if new_status in VALID_STATUSES:
                self.status = new_status
            self.last_updated = ev.get("ts", "")

        elif op == "MOVE":
            new_status = ev.get("status_to", "")
            if new_status in VALID_STATUSES:
                self.status = new_status
            if new_status == "DONE":
                self.done_ts = ev.get("ts", "")
            if ev.get("note"):
                self.note = ev["note"]
            self.last_updated = ev.get("ts", "")

        elif op == "UPDATE":
            for field in ("owner", "priority", "title", "note",
                          "proof_required", "blocked_on", "depends_on"):
                if field in ev:
                    setattr(self, field, ev[field])
            new_status = ev.get("status_to", "")
            if new_status in VALID_STATUSES:
                self.status = new_status
            self.last_updated = ev.get("ts", "")

        elif op == "PROOF_EXPECTED":
            self.proof_status   = "EXPECTED"
            self.proof_required = "YES"
            if ev.get("proof_items"):
                items = ev["proof_items"]
                self.proof_items = items if isinstance(items, list) else [str(items)]
            if ev.get("note"):
                self.note = ev["note"]
            self.last_updated = ev.get("ts", "")

        elif op == "PROOF_RECEIVED":
            self.proof_status = "RECEIVED"
            if ev.get("note"):
                self.note = ev["note"]
            self.last_updated = ev.get("ts", "")

        elif op == "COMMENT":
            if ev.get("note"):
                self.note = ev["note"]
            self.last_updated = ev.get("ts", "")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id":        self.task_id,
            "status":         self.status,
            "owner":          self.owner,
            "priority":       self.priority,
            "title":          self.title,
            "proof_required": self.proof_required,
            "proof_status":   self.proof_status,
            "proof_items":    self.proof_items,
            "blocked_on":     self.blocked_on,
            "depends_on":     self.depends_on,
            "note":           self.note,
            "done_ts":        self.done_ts,
            "last_updated":   self.last_updated,
        }

    def mtl_line(self) -> str:
        """Single-line MTL representation."""
        parts = [f"• {self.task_id}"]
        if self.title:
            parts.append(f"— {self.title}")
        if self.owner:
            parts.append(f"[{self.owner}]")
        if self.priority and self.status != "DONE":
            parts.append(f"P:{self.priority}")
        if self.blocked_on:
            parts.append(f"BLOCKED_ON:{self.blocked_on}")
        if self.proof_status == "EXPECTED":
            parts.append("⏳PROOF_EXPECTED")
        if self.note:
            # Truncate note to 60 chars for MTL
            note_short = self.note[:60] + ("..." if len(self.note) > 60 else "")
            parts.append(f"// {note_short}")
        return "  " + " ".join(parts)


# ─── State builder ────────────────────────────────────────────────────────────
def build_state(updates_path: Path, warnings: List[str]) -> Dict[str, Task]:
    tasks: Dict[str, Task] = {}

    if not updates_path.exists():
        warnings.append(f"mtl_updates.ndjson not found at {updates_path} — starting empty")
        return tasks

    events = []
    with open(updates_path) as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if "task_id" not in ev:
                    warnings.append(f"Line {i}: missing task_id, skipped")
                    continue
                if "op" not in ev:
                    warnings.append(f"Line {i}: missing op, skipped")
                    continue
                events.append(ev)
            except json.JSONDecodeError as e:
                warnings.append(f"Line {i}: JSON parse error: {e}")

    # Sort by ts (stable tie-breaker: file order via enumerate)
    events_sorted = sorted(enumerate(events), key=lambda x: (x[1].get("ts", ""), x[0]))

    for _, ev in events_sorted:
        tid = ev["task_id"]
        op  = ev.get("op", "").upper()

        if tid not in tasks:
            if op in ("MOVE", "UPDATE", "PROOF_EXPECTED", "PROOF_RECEIVED", "COMMENT"):
                warnings.append(f"task_id {tid!r} unknown for op {op!r} — created implicitly")
            tasks[tid] = Task(tid)

        tasks[tid].apply_event(ev, warnings)

    return tasks


# ─── Sort helpers ─────────────────────────────────────────────────────────────
def _sort_key_active(t: Task):
    return (PRIORITY_ORDER.get(t.priority, 9), t.task_id)


def _sort_key_done(t: Task):
    # Most recently done first
    return (t.done_ts or t.last_updated or "", t.task_id)


def _is_recent_done(t: Task, cutoff_ts: str) -> bool:
    ts = t.done_ts or t.last_updated or ""
    return ts >= cutoff_ts


# ─── MTL.md renderer ──────────────────────────────────────────────────────────
def render_mtl(tasks: Dict[str, Task], now_str: str) -> str:
    lines = [
        "================================",
        "MASTER TASK LIST — MTL",
        "Program: Agent911 Reliability Platform",
        "Owner: Chip (CHE10X)",
        f"Updated: {now_str}",
        "",
    ]

    # Cutoff: 14 days ago
    cutoff = _iso_days_ago(14)

    by_status: Dict[str, List[Task]] = {s: [] for s in STATUS_ORDER}
    for t in tasks.values():
        s = t.status
        if s == "DONE" and not _is_recent_done(t, cutoff):
            continue  # skip old done
        if s in by_status:
            by_status[s].append(t)

    # EXPECTED_PROOFS (tasks with proof_status=EXPECTED)
    expected = [t for t in tasks.values() if t.proof_status == "EXPECTED" and t.status != "DONE"]
    if expected:
        lines.append("EXPECTED_PROOFS")
        for t in sorted(expected, key=_sort_key_active):
            waiting = "Hendrik" if t.owner in ("GP-OPS","GP-PLM","GP-GTM","GP-WEB") else "Chip"
            lines.append(f"  • {t.task_id} — {t.title} → waiting on {waiting}")
        lines.append("")

    # ACTIVE
    lines.append("ACTIVE")
    active = sorted(by_status["ACTIVE"], key=_sort_key_active)
    if active:
        for t in active:
            lines.append(t.mtl_line())
    else:
        lines.append("  (none)")
    lines.append("")

    # BLOCKED
    lines.append("BLOCKED")
    blocked = sorted(by_status["BLOCKED"], key=lambda t: (0 if t.blocked_on=="CHIP" else 1, t.task_id))
    if blocked:
        for t in blocked:
            lines.append(t.mtl_line())
    else:
        lines.append("  (none)")
    lines.append("")

    # WATCH
    lines.append("WATCH")
    watch = sorted(by_status["WATCH"], key=_sort_key_active)
    if watch:
        for t in watch:
            lines.append(t.mtl_line())
    else:
        lines.append("  (none)")
    lines.append("")

    # DONE (last 14 days)
    lines.append("DONE (last 14 days)")
    done = sorted(by_status["DONE"], key=_sort_key_done, reverse=True)
    if done:
        for t in done:
            ts_short = t.done_ts[:10] if t.done_ts else "?"
            lines.append(f"  • {t.task_id} — {t.title} [{ts_short}]")
    else:
        lines.append("  (none)")
    lines.append("")
    lines.append("================================")
    return "\n".join(lines) + "\n"


# ─── DASHBOARD.md renderer ────────────────────────────────────────────────────
def render_dashboard(tasks: Dict[str, Task], now_str: str, warnings: List[str]) -> str:
    SEP = "---"
    OWNER_ORDER = ["GP-OPS", "GP-PLM", "GP-GTM", "GP-WEB", "CHIP", ""]

    cutoff_7d = _iso_days_ago(7)
    active   = [t for t in tasks.values() if t.status == "ACTIVE"]
    blocked  = [t for t in tasks.values() if t.status == "BLOCKED"]
    watch    = [t for t in tasks.values() if t.status == "WATCH"]
    done_7d  = [t for t in tasks.values()
                if t.status == "DONE" and _is_recent_done(t, cutoff_7d)]
    expected = [t for t in tasks.values()
                if t.proof_status == "EXPECTED" and t.status != "DONE"]

    lines = [
        "================================",
        "AGENT911 CONTROL PLANE — DASHBOARD",
        f"Updated: {now_str}",
        "",
        "SYSTEM SUMMARY",
        f"Active Tasks: {len(active)}",
        f"Blocked Tasks: {len(blocked)}",
        f"Expected Proofs: {len(expected)}",
        f"Recent Done (7d): {len(done_7d)}",
        "",
        SEP,
        "",
        "ACTIVE — BY OWNER",
        "",
    ]

    # Active by owner
    all_owners = sorted(set(t.owner for t in active),
                        key=lambda o: OWNER_ORDER.index(o) if o in OWNER_ORDER else 99)
    if active:
        for owner in all_owners:
            owner_tasks = sorted([t for t in active if t.owner == owner],
                                 key=_sort_key_active)
            if not owner_tasks:
                continue
            lines.append(owner or "UNASSIGNED")
            for t in owner_tasks:
                lines.append(f"\t• {t.task_id} — {t.title} — PRIORITY: {t.priority} — STATUS: {t.status}")
            lines.append("")
    else:
        lines.append("\t(none)")
        lines.append("")

    # Expected proofs
    lines += [SEP, "", "EXPECTED PROOFS", ""]
    waiting_hendrik = sorted(
        [t for t in expected if t.owner in ("GP-OPS","GP-PLM","GP-GTM","GP-WEB")],
        key=_sort_key_active)
    waiting_chip = sorted(
        [t for t in expected if t.owner == "CHIP"],
        key=_sort_key_active)

    lines.append("WAITING ON HENDRIK")
    if waiting_hendrik:
        for t in waiting_hendrik:
            lines.append(f"\t• {t.task_id} — {t.title}")
    else:
        lines.append("\t(none)")
    lines.append("")

    lines.append("WAITING ON CHIP")
    if waiting_chip:
        for t in waiting_chip:
            lines.append(f"\t• {t.task_id} — {t.title}")
    else:
        lines.append("\t(none)")
    lines.append("")

    # Blocked
    chip_blocked     = sorted([t for t in blocked if t.blocked_on == "CHIP"],
                               key=lambda t: t.task_id)
    external_blocked = sorted([t for t in blocked if t.blocked_on not in ("CHIP", "HENDRIK", "")],
                               key=lambda t: t.task_id)
    hendrik_blocked  = sorted([t for t in blocked if t.blocked_on == "HENDRIK"],
                               key=lambda t: t.task_id)

    lines += [SEP, "", "BLOCKED (ACTION REQUIRED)", ""]

    lines.append("BLOCKED ON CHIP")
    if chip_blocked:
        for t in chip_blocked:
            lines.append(f"\t• {t.task_id} — {t.title}")
    else:
        lines.append("\t(none)")
    lines.append("")

    other_ext = external_blocked + hendrik_blocked
    lines.append("BLOCKED ON EXTERNAL")
    if other_ext:
        for t in other_ext:
            label = t.blocked_on or "EXTERNAL"
            lines.append(f"\t• {t.task_id} — {t.title} [{label}]")
    else:
        lines.append("\t(none)")
    lines.append("")

    # Watch
    lines += [SEP, "", "WATCH (NO ACTION YET)"]
    if watch:
        for t in sorted(watch, key=_sort_key_active):
            lines.append(f"\t• {t.task_id} — {t.title}")
    else:
        lines.append("\t(none)")
    lines.append("")

    # Done last 7d
    lines += [SEP, "", "DONE — LAST 7 DAYS"]
    if done_7d:
        for t in sorted(done_7d, key=_sort_key_done, reverse=True):
            ts_short = t.done_ts[:10] if t.done_ts else "?"
            lines.append(f"\t• {t.task_id} — {t.title} — {ts_short}")
    else:
        lines.append("\t(none)")
    lines.append("")

    # Warnings
    lines += [SEP, "", "WARNINGS (if any)", ""]
    if warnings:
        for w in warnings:
            lines.append(f"\t{w}")
    else:
        lines.append("\t(none)")
    lines.append("")

    lines.append("================================")
    return "\n".join(lines) + "\n"


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _iso_days_ago(days: int) -> str:
    from datetime import timedelta
    dt = datetime.now(timezone.utc) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _now_utc_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    import argparse
    p = argparse.ArgumentParser(description="MTL + Dashboard generator")
    p.add_argument("--repo-root", default=None,
                   help="Path to repo root (default: auto-detect)")
    args = p.parse_args()

    root = Path(args.repo_root).resolve() if args.repo_root else DEFAULT_REPO_ROOT
    paths = _repo(root)

    # Ensure ops dir exists
    paths["mtl_out"].parent.mkdir(parents=True, exist_ok=True)

    warnings: List[str] = []
    now_str = _now_utc_str()

    print(f"mtl_apply_updates: reading {paths['mtl_updates']}", flush=True)

    # Build state from NDJSON
    tasks = build_state(paths["mtl_updates"], warnings)
    print(f"  tasks loaded: {len(tasks)}", flush=True)

    # Render outputs
    mtl_content       = render_mtl(tasks, now_str)
    dashboard_content = render_dashboard(tasks, now_str, warnings)

    # Write MTL.md
    paths["mtl_out"].write_text(mtl_content)
    print(f"  MTL.md written → {paths['mtl_out']}", flush=True)

    # Write DASHBOARD.md
    paths["dashboard"].write_text(dashboard_content)
    print(f"  DASHBOARD.md written → {paths['dashboard']}", flush=True)

    # Write MTL.snapshot.json
    snapshot = {
        "generated_at": now_str,
        "task_count":   len(tasks),
        "tasks":        {tid: t.to_dict() for tid, t in sorted(tasks.items())},
    }
    paths["snapshot"].write_text(json.dumps(snapshot, indent=2) + "\n")
    print(f"  MTL.snapshot.json written → {paths['snapshot']}", flush=True)

    # Mirror DASHBOARD to watchdog dir
    watchdog_mirror = Path.home() / ".openclaw" / "watchdog" / "DASHBOARD.md"
    try:
        watchdog_mirror.parent.mkdir(parents=True, exist_ok=True)
        watchdog_mirror.write_text(dashboard_content)
        print(f"  Mirror → {watchdog_mirror}", flush=True)
    except Exception as e:
        warnings.append(f"Mirror write failed: {e}")

    if warnings:
        print(f"  WARNINGS ({len(warnings)}):", flush=True)
        for w in warnings:
            print(f"    {w}", flush=True)

    print("mtl_apply_updates: DONE", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
