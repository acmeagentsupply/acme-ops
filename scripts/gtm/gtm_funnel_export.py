#!/usr/bin/env python3
"""
GTM Funnel Export Surface — File-Only v1
TASK_ID: A-FUN-P4-001
OWNER:   GP-OPS

Produces a deterministic, GTM-consumable weekly funnel bundle
in ~/.openclaw/watchdog/gtm_exports/ without network calls.

Inputs (read-only):
  ~/.openclaw/watchdog/gtm_funnel_weekly.json
  ~/.openclaw/watchdog/gtm_funnel_weekly.md

Outputs (deterministic):
  ~/.openclaw/watchdog/gtm_exports/funnel_export.json
  ~/.openclaw/watchdog/gtm_exports/funnel_export.md
  ~/.openclaw/watchdog/gtm_exports/export_manifest.json

Emits (append-only):
  GTM_EXPORT_READY → ops_events.log

SAFETY:
  - No network calls (v1)
  - No openclaw.json writes
  - Zero gateway restarts
  - Append-only ops_events.log
  - Exits 0 always
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HOME     = os.path.expanduser("~")
WATCHDOG = os.path.join(HOME, ".openclaw", "watchdog")

SRC_FUNNEL_JSON = os.path.join(WATCHDOG, "gtm_funnel_weekly.json")
SRC_FUNNEL_MD   = os.path.join(WATCHDOG, "gtm_funnel_weekly.md")
SRC_OPS         = os.path.join(WATCHDOG, "ops_events.log")

OUT_EXPORT_DIR  = os.path.join(WATCHDOG, "gtm_exports")
OUT_JSON        = os.path.join(OUT_EXPORT_DIR, "funnel_export.json")
OUT_MD          = os.path.join(OUT_EXPORT_DIR, "funnel_export.md")
OUT_MANIFEST    = os.path.join(OUT_EXPORT_DIR, "export_manifest.json")

# Fixed manifest key order (deterministic insertion order)
_MANIFEST_KEY_ORDER = [
    "generated_ts",
    "window_days",
    "attach_rate",
    "expansion_rate",
    "system_id",
    "source_json_hash",
    "source_md_hash",
    "export_status",
]

EVT_EXPORT_READY = "GTM_EXPORT_READY"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _safe_read_text(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


def _safe_json_load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _sha256_short(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _append_event(path: str, record: dict) -> bool:
    try:
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Core export
# ---------------------------------------------------------------------------

def run_export(
    json_src: str = SRC_FUNNEL_JSON,
    md_src: str   = SRC_FUNNEL_MD,
    out_dir: str  = OUT_EXPORT_DIR,
    ops_path: str = SRC_OPS,
    hostname: str = "",
) -> dict:
    """
    Read source files, write deterministic export bundle, emit NDJSON.

    Determinism: generated_ts is taken from the source JSON's own 'ts'
    field (not datetime.now()), so same source files → identical bundle.

    Returns dict with status, file paths, hashes.
    Exits 0 always.
    """
    if not hostname:
        hostname = socket.gethostname()

    status = "NONE"
    result = {
        "status":           status,
        "export_dir":       out_dir,
        "json_hash":        "",
        "md_hash":          "",
        "manifest_written": False,
        "event_emitted":    False,
    }

    # ── Read sources ──────────────────────────────────────────────────────
    json_content = _safe_read_text(json_src)
    md_content   = _safe_read_text(md_src)

    if not json_content and not md_content:
        result["status"] = "NONE"
        return result

    # ── Parse JSON to extract manifest fields ─────────────────────────────
    source_data    = _safe_json_load(json_src) if json_content else {}
    # Use source JSON's ts as generated_ts for determinism
    generated_ts   = source_data.get("ts", _ts_iso())
    window_days    = source_data.get("window_days",            7)
    attach_rate    = source_data.get("sentinel_attach_rate",   0.0)
    expansion_rate = source_data.get("agent911_expansion_rate", 0.0)

    json_hash = _sha256_short(json_content)
    md_hash   = _sha256_short(md_content)

    # ── Create export directory ───────────────────────────────────────────
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        result["status"] = "NONE"
        return result

    # ── Write funnel_export.json (copy) ───────────────────────────────────
    try:
        with open(os.path.join(out_dir, "funnel_export.json"), "w",
                  encoding="utf-8") as fh:
            fh.write(json_content)
    except Exception:
        pass

    # ── Write funnel_export.md (copy) ─────────────────────────────────────
    try:
        with open(os.path.join(out_dir, "funnel_export.md"), "w",
                  encoding="utf-8") as fh:
            fh.write(md_content)
    except Exception:
        pass

    # ── Build manifest (fixed key order for determinism) ──────────────────
    manifest: dict = {}
    manifest["generated_ts"]      = generated_ts      # from source JSON
    manifest["window_days"]       = window_days
    manifest["attach_rate"]       = attach_rate
    manifest["expansion_rate"]    = expansion_rate
    manifest["system_id"]         = hostname
    manifest["source_json_hash"]  = json_hash
    manifest["source_md_hash"]    = md_hash
    manifest["export_status"]     = "READY"

    manifest_ok = False
    try:
        with open(os.path.join(out_dir, "export_manifest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2)
            fh.write("\n")
        manifest_ok = True
    except Exception:
        pass

    status = "READY" if manifest_ok else "PARTIAL"

    # ── Emit GTM_EXPORT_READY ─────────────────────────────────────────────
    emit_ts  = _ts_iso()
    evt_ok   = _append_event(ops_path, {
        "ts":              emit_ts,
        "event":           EVT_EXPORT_READY,
        "severity":        "INFO",
        "source":          "gtm_funnel_export",
        "export_status":   status,
        "attach_rate":     attach_rate,
        "expansion_rate":  expansion_rate,
        "json_hash":       json_hash,
        "md_hash":         md_hash,
        "export_dir":      out_dir,
    })

    result.update({
        "status":           status,
        "json_hash":        json_hash,
        "md_hash":          md_hash,
        "manifest_written": manifest_ok,
        "event_emitted":    evt_ok,
    })
    return result


# ---------------------------------------------------------------------------
# Manifest reader (for dashboard/state integration)
# ---------------------------------------------------------------------------

def read_export_status(out_dir: str = OUT_EXPORT_DIR) -> str:
    """Return 'READY' if export_manifest.json exists and is readable, else 'NONE'."""
    manifest_path = os.path.join(out_dir, "export_manifest.json")
    try:
        data = _safe_json_load(manifest_path)
        if data.get("export_status") == "READY":
            return "READY"
    except Exception:
        pass
    return "NONE"


# ---------------------------------------------------------------------------
# Standalone CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import time

    t0      = time.monotonic()
    result  = run_export()
    elapsed = round((time.monotonic() - t0) * 1000, 2)

    print(f"GTM_EXPORT elapsed={elapsed}ms status={result['status']}")
    print(f"  json_hash:  {result['json_hash']}")
    print(f"  md_hash:    {result['md_hash']}")
    print(f"  manifest:   {result['manifest_written']}")
    print(f"  event:      {result['event_emitted']}")
    print(f"  dir:        {OUT_EXPORT_DIR}")
    sys.exit(0)
