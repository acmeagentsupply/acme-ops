#!/usr/bin/env python3
"""
Bonfire REB Consumer — PROJ-2026-009
Tails the Resilience Event Bus and maps HIGH/CRITICAL events into Bonfire risk signals.

Run trigger: standalone cron or launchd (independent of session_log_feeder).
Recommended interval: 60 seconds.

Usage:
    python3 reb_consumer.py [--dry-run] [--since-hours N]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Bonfire path
SCRIPT_DIR = Path(__file__).parent.resolve()
BONFIRE_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(BONFIRE_ROOT.parent))

# REB path (acme-ops)
ACME_OPS = Path.home() / ".openclaw" / "workspace" / "acme-ops"
if str(ACME_OPS) not in sys.path:
    sys.path.insert(0, str(ACME_OPS))

REB_FILE = Path.home() / ".openclaw" / "resilience" / "resilience_events.jsonl"
CONSUMER_STATE = Path.home() / ".openclaw" / "logs" / "reb_consumer_state.json"

BONFIRE_AVAILABLE = False
try:
    from bonfire.risk.agent_risk_score import record_request as record_risk_request
    BONFIRE_AVAILABLE = True
except ImportError:
    pass

# Severity levels that Bonfire acts on
ACTIONABLE_SEVERITIES = {"HIGH", "CRITICAL"}

# Map REB sources to agent_id for Bonfire risk scoring
SOURCE_TO_AGENT = {
    "sentinel":   "agent:sentinel:main",
    "infrawatch": "agent:infrawatch:main",
    "watchdog":   "agent:watchdog:main",
    "lazarus":    "agent:lazarus:main",
    "agent911":   "agent:agent911:main",
}


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_state() -> dict:
    """Load last-processed timestamp per source."""
    if CONSUMER_STATE.exists():
        try:
            return json.loads(CONSUMER_STATE.read_text())
        except Exception:
            pass
    return {"last_ts": None, "events_processed": 0}


def _save_state(state: dict) -> None:
    CONSUMER_STATE.parent.mkdir(parents=True, exist_ok=True)
    CONSUMER_STATE.write_text(json.dumps(state, indent=2))


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    ts = raw.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _read_new_events(since_ts: str | None, since_hours: int | None = None) -> list[dict]:
    """Read events from REB file, filtered by timestamp."""
    if not REB_FILE.exists():
        return []

    cutoff = None
    if since_hours is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    elif since_ts:
        cutoff = _parse_ts(since_ts)

    events = []
    with open(REB_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if cutoff:
                event_ts = _parse_ts(str(event.get("ts", "")))
                if event_ts and event_ts <= cutoff:
                    continue

            events.append(event)

    return events


def _map_to_bonfire_risk(event: dict, dry_run: bool = False) -> bool:
    """
    Map a HIGH/CRITICAL REB event to a Bonfire risk signal.
    Returns True if processed successfully.
    """
    source = event.get("source", "unknown")
    severity = event.get("severity", "INFO")
    event_type = event.get("event_type", "unknown")
    payload = event.get("payload", {})

    if severity not in ACTIONABLE_SEVERITIES:
        return False

    agent_id = SOURCE_TO_AGENT.get(source, f"agent:{source}:main")

    # Map severity to a synthetic token cost for risk scoring
    # CRITICAL = high synthetic load, HIGH = moderate
    synthetic_tokens = 10000 if severity == "CRITICAL" else 5000

    if dry_run:
        print(f"[dry-run] Would emit risk signal: agent={agent_id} "
              f"event={event_type} severity={severity} tokens={synthetic_tokens}")
        return True

    if not BONFIRE_AVAILABLE:
        print(f"[reb_consumer] Bonfire unavailable — logged: {source}/{event_type}/{severity}")
        return False

    try:
        record_risk_request(
            agent_id=agent_id,
            session_id=f"reb-{event.get('ts', 'unknown')}",
            model="reb-signal",
            total_tokens=synthetic_tokens,
            prompt_tokens=synthetic_tokens,
            completion_tokens=0,
            latency_ms=0,
            status=f"reb_{severity.lower()}",
            lane="resilience",
        )
        return True
    except Exception as e:
        print(f"[reb_consumer] risk signal failed: {e}", file=sys.stderr)
        return False


def run(dry_run: bool = False, since_hours: int | None = None) -> dict:
    """Main consumer run. Returns summary dict."""
    state = _load_state()
    since_ts = state.get("last_ts")

    events = _read_new_events(since_ts=since_ts, since_hours=since_hours)

    processed = 0
    skipped = 0
    failed = 0
    latest_ts = since_ts

    for event in events:
        severity = event.get("severity", "INFO")
        ts = str(event.get("ts", ""))

        if severity in ACTIONABLE_SEVERITIES:
            ok = _map_to_bonfire_risk(event, dry_run=dry_run)
            if ok:
                processed += 1
            else:
                failed += 1
        else:
            skipped += 1

        if ts and (latest_ts is None or ts > latest_ts):
            latest_ts = ts

    if not dry_run and latest_ts:
        state["last_ts"] = latest_ts
        state["events_processed"] = state.get("events_processed", 0) + processed
        _save_state(state)

    summary = {
        "ts": _iso_now(),
        "events_seen": len(events),
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "bonfire_available": BONFIRE_AVAILABLE,
        "dry_run": dry_run,
    }
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bonfire REB Consumer")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    parser.add_argument("--since-hours", type=int, default=None,
                        help="Process events from last N hours (overrides state)")
    args = parser.parse_args()

    result = run(dry_run=args.dry_run, since_hours=args.since_hours)
    print(json.dumps(result, indent=2))
    sys.exit(0 if result["failed"] == 0 else 1)
