#!/usr/bin/env bash
set -euo pipefail

echo "[healthcheck] Checking OpenClaw gateway..."
curl -fsSI http://127.0.0.1:18789/ >/dev/null && echo "  - gateway: OK (HTTP 200)" || echo "  - gateway: NOT OK"

echo "[healthcheck] Checking gog listener..."
if lsof -nP -iTCP:8788 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "  - gog: LISTENING on 8788"
else
  echo "  - gog: NOT LISTENING on 8788"
fi

echo "[healthcheck] Done."
