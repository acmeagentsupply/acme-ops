# openclaw-ops

Operational runbooks + automation for an OpenClaw-based personal/agent system.

## What this repo contains
- **scripts/**: health checks, auto-heal utilities, diagnostics
- **launchd/**: LaunchAgent plists (no secrets)
- **docs/**: integration guides + DR runbooks
- **templates/**: sanitized configuration examples (`*.example`)

## What this repo MUST NOT contain
Never commit:
- `~/.openclaw/openclaw.json`
- `~/.openclaw/**/auth-profiles.json`
- Any `*.env` with real values
- Any credentials JSON (Google service accounts, OAuth, etc.)
- Tokens/keys in any docs

Use:
- **Keychain** for secrets when possible
- **Local env files** under `~/.openclaw/env/` (gitignored) otherwise

## Quick start (local machine)
```bash
git clone <your-private-repo-url> openclaw-ops
cd openclaw-ops
./bin/bootstrap.sh
```

`bootstrap.sh` installs LaunchAgents (if present), validates paths, and runs a light health check.

## Conventions
- Bash scripts are **strict**: `set -euo pipefail`
- Always print safe diagnostics; **never echo secrets**
- Prefer reading tokens from:
  1) env vars (process-local)
  2) `~/.openclaw/env/*.env` (0600)
  3) macOS Keychain (optional helper)

## Suggested release tags
- `ops-v1.0` baseline
- `gmail-v3.3` Gmail integration update
- `dr-v2.0` DR/Recovery update

## Recent release notes
- Watchdog v1.1 (2026-03-03): hygiene guard folded into owned Watchdog capability, `restore_staging` governance added, Sentinel disk pressure surfaced, OCTriage footprint capture added. Canonical note: [`docs/releases/watchdog-v1.1-release-notes.md`](./docs/releases/watchdog-v1.1-release-notes.md)
