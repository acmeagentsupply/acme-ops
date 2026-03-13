# Scout Stub — Archived 2026-02-24

These files were the original (placeholder) implementation of the OpenClaw Scout.
Replaced by a native `openclaw cron` job on 2026-02-24.

## Why Replaced

The shell script approach used placeholder logic — no real research was being done.
`openclaw cron` dispatches an actual agent turn (isolated session, full tool access:
web_fetch, write, message, etc.) which is the correct layer for this task.

## Replacement

Job name: `scout-oc-daily`  
Job ID: `5d555b89-95cf-436b-821d-3fff3cffe3ce`  
Schedule: `30 7 * * * @ America/New_York`  
Created via: `openclaw cron add --name scout-oc-daily ...`

The prompt is stored in the gateway cron config (view with `openclaw cron list --json`).
