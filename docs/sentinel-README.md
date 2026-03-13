# Sentinel — Operator Note

Sentinel provides advisory-only protection signals for local OpenClaw runtime health. It must never block the watchdog loop or halt a Sentinel cycle.

## Disk Pressure Signal

- Signal: `sentinel_disk_pressure`
- Emitted once per Sentinel cycle
- Source command: `du -sk ~/.openclaw/watchdog`
- Event fields: `disk_mb`, `growth_mb_per_hr`, `pressure_state`, `time_to_pressure_hrs`, `advisory_only`
- Legacy state fields retained in `sentinel_protection_state.json`: `disk_mb`, `growth_mb_per_hr`, `pressure_level`
- Predictive state file: `~/.openclaw/watchdog/sentinel_predictive_state.json`
- `pressure_state=normal`: watchdog footprint `<1024 MB` and smoothed growth `<150 MB/hr`
- `pressure_state=rising`: watchdog footprint `1024-2500 MB` or smoothed growth `150-300 MB/hr`
- `pressure_state=critical`: watchdog footprint `>2500 MB` or smoothed growth `>300 MB/hr`
- `time_to_pressure_hrs`: hours to reach 80% of the 6 GB ceiling (`4915.2 MB`) at current smoothed growth; `null` when stable/non-growing
- Behavior: advisory-only, non-blocking, exit-safe

## Operator Meaning

- `normal`: current watchdog footprint and growth are below advisory thresholds
- `rising`: storage hygiene should be reviewed before compaction pressure rises
- `critical`: watchdog storage growth is materially elevated and should be investigated

## Upgrade Note

- v1.1 exposed advisory disk size and point-in-time growth
- v1.2 adds 3-point growth smoothing, `pressure_state`, and `time_to_pressure_hrs`
- v1.2 writes predictive disk state to `sentinel_predictive_state.json` each cycle for downstream correlators and support bundles

## Safety

- No outbound network calls
- No daemon management
- No config writes
- Writes only Sentinel state and append-only ops events
