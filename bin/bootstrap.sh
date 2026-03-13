#!/usr/bin/env bash
set -euo pipefail

echo "== openclaw-ops bootstrap =="

# 1) Create local folders
mkdir -p "${HOME}/.openclaw/bin" "${HOME}/.openclaw/env" "/tmp/openclaw"

# 2) Install scripts (copy stubs into ~/.openclaw/bin if not present)
install_script() {
  local src="$1"
  local dst="${HOME}/.openclaw/bin/$(basename "$src")"
  if [[ -f "$dst" ]]; then
    echo " - exists: $dst (leaving as-is)"
  else
    cp "$src" "$dst"
    chmod 755 "$dst"
    echo " - installed: $dst"
  fi
}

install_script "./scripts/gmail_heartbeat_sentinel.sh"
install_script "./scripts/healthcheck.sh"
install_script "./scripts/openclaw_gmail_autoheal.sh"

# 3) Install LaunchAgent (optional)
PLIST_SRC="./launchd/ai.openclaw.gmail_sentinel.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/ai.openclaw.gmail_sentinel.plist"

if [[ -f "$PLIST_DST" ]]; then
  echo " - LaunchAgent exists: $PLIST_DST (leaving as-is)"
else
  cp "$PLIST_SRC" "$PLIST_DST"
  echo " - Installed LaunchAgent: $PLIST_DST"
fi

echo
echo "Next (manual):"
echo "  1) Create env file from template:"
echo "     cp ./templates/gmail_sentinel.env.example ${HOME}/.openclaw/env/gmail_sentinel.env"
echo "     chmod 600 ${HOME}/.openclaw/env/gmail_sentinel.env"
echo "     edit values (HOOKS_TOKEN, EXTERNAL_URL_BASE, etc.)"
echo
echo "  2) Load LaunchAgent (optional):"
echo "     launchctl bootout gui/$UID/ai.openclaw.gmail_sentinel 2>/dev/null || true"
echo "     launchctl bootstrap gui/$UID ${HOME}/Library/LaunchAgents/ai.openclaw.gmail_sentinel.plist"
echo
echo "  3) Run healthcheck:"
echo "     ${HOME}/.openclaw/bin/healthcheck.sh"
echo
echo "Done."
