WATCHDOG v1.1 - RELEASE NOTE
Status: Production Patch
Owner: Control Plane / Reliability
Date: 2026-03-03

Summary

Watchdog v1.1 closes a critical real-world gap observed during live operator use.

Based on internal telemetry and field behavior, Watchdog has been expanded to actively govern disk growth,
watchdog log bloat, and restore staging pressure - conditions that materially impact OpenClaw stability.

This release converts previously manual hygiene work into automated, continuously enforced guardrails.

Why This Matters

During sustained OpenClaw operation, the following pressure points were observed:
- Watchdog directory growth accumulating silently
- restore_staging scratch space persisting longer than expected
- Log surfaces growing without early operator signal
- Disk pressure emerging before existing guards fired

While RadCheck, Sentinel, and Agent911 remained healthy, they were not yet watching the surfaces
that produced real operator pain.

Watchdog v1.1 addresses that gap directly.

What's New in v1.1

1. Hygiene Guard folded into Watchdog
   Previously standalone hygiene guard is now an owned Watchdog capability.
   New behavior: runs every 15 minutes via LaunchAgent, single-pass deterministic cleanup,
   no duplicate logging, safe skip behavior when components are active,
   version stamped via WATCHDOG_HYGIENE_VERSION=1.1.
   This converts reactive cleanup into continuous hygiene enforcement.

2. Sentinel Disk Pressure Signal
   New advisory signal: SENTINEL_DISK_PRESSURE
   Emits: disk_mb, growth_mb_per_hr, pressure_level
   Purpose: early visibility into disk growth slope, operator awareness before hard pressure events,
   foundation for future predictive guardrails.
   This expands Sentinel's observability surface based on real operator telemetry.

3. OCTriageUnit Expansion
   OCTriageUnit now captures watchdog footprint directly.
   New bundle artifact: watchdog_disk_usage.txt
   New bundle flag: watchdog_bloat_warning=true when watchdog >500MB
   Performance impact: <200ms added to triage runtime.
   Operators can now detect watchdog bloat from a single triage run.

4. restore_staging Governance
   Watchdog now actively governs restore staging growth.
   Behavior: warn at 100MB, prune at 250MB, skip when active lock detected.
   Component label: restore_staging_guard
   Prevents silent accumulation of recovery scratch data.

Operational Impact
- Lower steady-state disk growth
- Faster compaction behavior
- Earlier visibility into pressure conditions
- Reduced manual firefighting
- No breaking changes. All behavior is backward compatible.

Safety Posture
Watchdog v1.1 remains: read-only outside its owned surfaces, deterministic per pass,
non-blocking to the control plane, fully observable via logs.
The hygiene loop is intentionally conservative.

Forward Direction
Observability surfaces will continue to expand based on real operator telemetry.
Expect further tightening in: growth slope detection, pre-compaction pressure signals,
cross-surface correlation.

Operator Action
No action required. Watchdog v1.1 is active once deployed.
Operators may verify via:
  launchctl print gui/$UID/ai.openclaw.hygiene_guard
  octriageunit
