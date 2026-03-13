#!/usr/bin/env python3
"""
Stall Detector - scans recent session JSONL files for slow model calls.

Writes MODEL_STALL_WARN / MODEL_STALL_HARD / MODEL_FAILOVER_TRIGGERED lines to stall_log.
Deduplicates per req_id via stall_seen_file.
Prints "provider|age_s|status" to stdout for heartbeat enrichment.

Usage:
  stall_detector.py <sessions_dir> <stall_log> <stall_seen_file> <warn_ms> <hard_ms> <ts_now...>
"""
import json
import os
import sys
import time
from datetime import datetime, timezone

sessions_dir   = sys.argv[1]
stall_log      = sys.argv[2]
stall_seen_file = sys.argv[3]
warn_ms        = int(sys.argv[4])
hard_ms        = int(sys.argv[5])
ts_now         = " ".join(sys.argv[6:])  # timestamp may contain spaces

now_ts = time.time()
LOOKBACK_SECS = 700  # slightly more than one watchdog cycle (300s + runtime buffer)

# ── Load dedup set ────────────────────────────────────────────────────────────
seen = set()
if os.path.exists(stall_seen_file):
    try:
        with open(stall_seen_file) as f:
            seen = set(line.strip() for line in f if line.strip())
    except Exception:
        pass

new_seen   = []
stall_lines = []
last_model  = {
    "ts": 0.0,
    "provider": "unknown",
    "model": "unknown",
    "age_s": -1,
    "status": "unknown",
}

# ── Scan session files ────────────────────────────────────────────────────────
if os.path.isdir(sessions_dir):
    for fn in sorted(os.listdir(sessions_dir)):
        if not fn.endswith(".jsonl"):
            continue
        fp = os.path.join(sessions_dir, fn)
        try:
            mtime = os.path.getmtime(fp)
        except Exception:
            continue
        if now_ts - mtime > LOOKBACK_SECS:
            continue

        session_id = fn[:-6]  # strip ".jsonl"

        try:
            with open(fp) as f:
                objs = [json.loads(line) for line in f if line.strip()]
        except Exception:
            continue

        # Build index: assistant message id -> (provider, model)  for failover detection
        asst_models = {}
        model_changes = []

        pending_user = None
        last_hard_req = None

        for obj in objs:
            otype = obj.get("type", "")

            # Track model changes (potential failover indicators)
            if otype == "model_change":
                model_changes.append({
                    "ts": obj.get("timestamp", ""),
                    "provider": obj.get("provider", "unknown"),
                    "model": obj.get("modelId", "unknown"),
                })
                continue

            if otype != "message":
                continue

            msg  = obj.get("message", {})
            role = msg.get("role", "")

            if role == "user":
                pending_user = obj
                continue

            if role == "assistant" and pending_user is not None:
                try:
                    t_u = datetime.fromisoformat(
                        pending_user["timestamp"].replace("Z", "+00:00")
                    ).timestamp()
                    t_a = datetime.fromisoformat(
                        obj["timestamp"].replace("Z", "+00:00")
                    ).timestamp()
                    dur_ms = int((t_a - t_u) * 1000)

                    if dur_ms < 0:
                        pending_user = None
                        continue

                    provider = msg.get("provider", "unknown")
                    model_id = msg.get("model", "unknown")
                    req_id   = f"{session_id}:{obj.get('id', 'x')}"

                    # Update last-model tracker
                    if t_a > last_model["ts"]:
                        last_model.update({
                            "ts":       t_a,
                            "provider": provider,
                            "model":    model_id,
                            "age_s":    int(now_ts - t_a),
                            "status":   "timeout" if dur_ms >= hard_ms else "ok",
                        })

                    # Emit stall lines — WARN always fires at >=warn_ms;
                    # HARD additionally fires at >=hard_ms. Both use same req_id
                    # but different dedup keys so both appear exactly once.
                    warn_key = f"warn:{req_id}"
                    hard_key = f"hard:{req_id}"

                    if dur_ms >= warn_ms and warn_key not in seen:
                        stall_lines.append(
                            f"[{ts_now}] MODEL_STALL_WARN"
                            f" provider={provider}"
                            f" duration_ms={dur_ms}"
                            f" model={model_id}"
                            f" req={req_id}"
                        )
                        new_seen.append(warn_key)
                        seen.add(warn_key)

                    if dur_ms >= hard_ms and hard_key not in seen:
                        stall_lines.append(
                            f"[{ts_now}] MODEL_STALL_HARD"
                            f" provider={provider}"
                            f" duration_ms={dur_ms}"
                            f" model={model_id}"
                            f" req={req_id}"
                        )
                        new_seen.append(hard_key)
                        seen.add(hard_key)
                        last_hard_req = {
                            "provider": provider,
                            "model":    model_id,
                            "req_id":   req_id,
                        }

                    # Detect failover: after a HARD stall in this session,
                    # if the next assistant message uses a different provider → emit FAILOVER
                    elif last_hard_req is not None and provider != last_hard_req["provider"]:
                        failover_key = f"fo:{session_id}:{obj.get('id','x')}"
                        if failover_key not in seen:
                            from_str = f"{last_hard_req['provider']}/{last_hard_req['model']}"
                            to_str   = f"{provider}/{model_id}"
                            stall_lines.append(
                                f"[{ts_now}] MODEL_FAILOVER_TRIGGERED"
                                f" from={from_str}"
                                f" to={to_str}"
                                f" req={last_hard_req['req_id']}"
                            )
                            new_seen.append(failover_key)
                            seen.add(failover_key)
                        last_hard_req = None

                except Exception:
                    pass

                pending_user = None

# ── Write stall log ───────────────────────────────────────────────────────────
if stall_lines:
    try:
        with open(stall_log, "a") as f:
            for line in stall_lines:
                f.write(line + "\n")
    except Exception:
        pass

# ── Persist new dedup entries ─────────────────────────────────────────────────
if new_seen:
    try:
        with open(stall_seen_file, "a") as f:
            for rid in new_seen:
                f.write(rid + "\n")
    except Exception:
        pass

# ── Merge model_state.json from model_router.py (prefer most recent) ─────────
model_state_path = os.path.join(os.path.dirname(stall_log), "model_state.json")
if os.path.exists(model_state_path):
    try:
        import json as _json
        with open(model_state_path) as _f:
            ms = _json.load(_f)
        ms_ts = float(ms.get("updated_at", 0))
        if ms_ts > last_model["ts"]:
            last_model["provider"] = ms.get("provider", "unknown")
            last_model["status"]   = ms.get("status",   "unknown")
            last_model["age_s"]    = int(now_ts - ms_ts)
    except Exception:
        pass

# ── Emit last-model stats for heartbeat (stdout) ──────────────────────────────
print(
    f"{last_model['provider']}|{last_model['age_s']}|{last_model['status']}",
    end=""
)
