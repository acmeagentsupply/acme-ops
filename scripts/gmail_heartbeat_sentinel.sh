#!/usr/bin/env bash
set -euo pipefail

# Gmail Heartbeat Sentinel (stub)
# Reads ~/.openclaw/env/gmail_sentinel.env if present.

ENV_FILE="${HOME}/.openclaw/env/gmail_sentinel.env"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi

: "${HOOKS_TOKEN:=REPLACE_ME}"
: "${EXTERNAL_URL_BASE:=https://REPLACE_ME.tailXXXX.ts.net}"
: "${GMAIL_PATH:=/gmail-pubsub}"
: "${GMAIL_PORT:=8788}"

echo "[sentinel] External endpoint should be:"
echo "  ${EXTERNAL_URL_BASE}${GMAIL_PATH}?token=REDACTED"
echo "[sentinel] (Replace this stub with gmail_heartbeat_sentinel_v2_phase1.sh)"
