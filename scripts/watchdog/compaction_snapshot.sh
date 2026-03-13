#!/usr/bin/env bash
# compaction_snapshot.sh — lightweight system pressure snapshot
# Single python3 process, no subprocesses — targets <250ms on macOS
# Usage: bash compaction_snapshot.sh [label]
# NOTE: mem/proc stats skipped to hit <250ms; load+gw are primary signals

LABEL="${1:-manual}"
WATCHDOG_DIR="$HOME/.openclaw/watchdog"
mkdir -p "$WATCHDOG_DIR"

python3 - "$LABEL" "$WATCHDOG_DIR" <<'PYEOF'
import sys, os, socket, json
from datetime import datetime, timezone

label       = sys.argv[1]
wdir        = sys.argv[2]
metrics_log = os.path.join(wdir, "compaction_metrics.log")
ts          = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# Load averages — syscall, instant
la1, la5, la15 = os.getloadavg()
load = f"{la1:.2f},{la5:.2f},{la15:.2f}"

# Gateway port — socket connect, no subprocess
gw_status = "down"
try:
    with socket.create_connection(("127.0.0.1", 18789), timeout=0.15):
        gw_status = "up"
except Exception:
    pass

line = json.dumps({
    "ts": ts, "label": label,
    "load_1_5_15": load,
    "gw_port18789": gw_status,
})

with open(metrics_log, "a") as f:
    f.write(line + "\n")

print(f"SNAPSHOT OK: {line}")
PYEOF
