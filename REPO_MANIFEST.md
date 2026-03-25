# REPO_MANIFEST.md — acmeagentsupply/acme-ops
_Last updated: 2026-03-24 | Owner: Soren Pagurus (PM)_

This file documents what is in this public repository and why.
The split between public and private is a product decision, not just a technical one.

**Rule: free-tier and free-base scope only. Pro features, paid products, and internal tooling live in `acme-ops-private`.**

Any change to the public/private split requires PM sign-off before execution. Engineering executes; PM decides.

---

## What's Here (Public — Free Tier)

| Path | Product | Notes |
|------|---------|-------|
| `scripts/radiation/` | RadCheck | Free reliability scanner. ACME Freeware License. |
| `scripts/sentinel/` | Sentinel | Free-tier detection scripts (attach bridge, funnel alignment). |
| `scripts/watchdog/` | Sentinel | Watchdog bridge scripts (compaction, silence, predictive guard). Sentinel-bundled free tier. |
| `scripts/lazarus/` | Lazarus | Bundled free with Agent911 purchase. |
| `scripts/install/` | All | Customer-facing installer. Must always be public. |
| `bonfire/collector/` | Bonfire | Free observability collection layer. |
| `bonfire/runtime/` | Bonfire | Free-tier runtime (throttle, model guard). |
| `bonfire/cli/` | Bonfire | Free CLI. |
| `bonfire/bonfire_logger.py` | Bonfire | Free logger. |
| `bonfire/budgets/` | Bonfire | Free budget tracking. |
| `bonfire/dashboard/` | Bonfire | Simple local operator view. File-backed. **Not** the full QM dashboard. |
| `bonfire/docs/` | Bonfire | Free-tier product docs. |
| `templates/` | All | Config examples for operators. |
| `launchd/` | All | Install launchd plists. Operator-facing. |
| `docs/` | All | Customer-facing docs for free products only (see docs section below). |
| `LICENSE` | — | ACME Freeware License v1. Covers all free-tier content in this repo. |
| `README.md`, `SECURITY.md`, `REPO_DOCTRINE.md` | — | Repo metadata. |

---

## What's NOT Here (Private — acme-ops-private)

| Content | Why Private |
|---------|-------------|
| `scripts/agent911/`, `agent911/` | Paid product ($19/mo) |
| `scripts/sphinxgate/` | Paid product ($5/mo) |
| `scripts/watchdog/transmission_router.py` + config + tests | Patent-sensitive IP (provisional 64/006,406 filed 2026-03-15) |
| `bonfire/router/`, `bonfire/governor/` | Patent-sensitive IP (same provisional) |
| `bonfire/analyzer/`, `forecast/`, `optimizer/`, `policy/`, `predictor/`, `risk/` | Paid Bonfire pro tier |
| `scripts/backup/` | Internal ops tooling — no customer value |
| `scripts/gtm/`, `scripts/funnel/`, `gtm/` | Internal GTM tooling |
| `scripts/support/` | Internal support tooling |
| `scripts/operator/`, `scripts/ops/`, `ops/` | Internal ops data |
| `config/openclaw.json` | Internal config artifact |
| `archive/` | Internal stubs |
| `docs/agent911_v0_1.md` | Paid product architecture doc |
| `docs/sphinxgate_runbook.md` | Paid product runbook |
| `docs/freeze_blockers_notes.md` | Internal build notes |
| `docs/config_template_REDACTED.json` | Internal config |
| `docs/project_operating_doctrine_addendum_mtl.md` | Internal ops |
| `docs/support_playbook_v0.md` | Internal support |
| `docs/DOCUMENT_ROUTING_DOCTRINE.md`, `docs/OPERATOR_LOG_INDEX_SYSTEM.md` | Internal |
| `docs/control-plane-watchdog-env-requirements.md` | Internal |
| `docs/gateway_compaction_report_v1.md` | Internal |

---

## Public Docs — What's Allowed Here

Only docs for free-tier products. Currently allowed:

- `docs/radiation_check_v1.md` — RadCheck
- `docs/sentinel-README.md` — Sentinel
- `docs/lazarus_protocol_v1.md` — Lazarus
- `docs/compaction_mitigation_v1.md` — Sentinel/operator utility
- `docs/openclaw_watchdog_runbook.md` — Watchdog (operator-facing)
- `docs/model_failover_runbook.md` — Generic operator guidance
- `docs/octriageunit-README.md` + `octriageunit-trust-doctrine.md` + `octriageunit-example-run.png` — Triage (OSS)
- `docs/stall_detector_runbook.md` — Operator utility
- `docs/watchdog-hygiene.md` — Operator utility
- `docs/README.md` — Index

---

## Product Tier Reference

| Product | Free base | Paid tier | Install path |
|---------|-----------|-----------|--------------|
| RadCheck | ✅ Free forever | RadCheck Pro (future) | `curl acmeagentsupply.com/install/radcheck \| bash` |
| Triage | ✅ OSS | Triage for Acme (commercial) | `curl acmeagentsupply.com/install/triage \| bash` |
| Sentinel | ✅ Base scripts free | Sentinel Pro (future) | `acme_install.sh --bundle sentinel` |
| Lazarus | ✅ Bundled with Agent911 | — | `acme_install.sh --bundle lazarus` |
| Agent911 | ❌ Paid ($19/mo) | — | Licensed install (coming) |
| SphinxGate | ❌ Paid ($5/mo) | — | Licensed install (coming) |
| Transmission | ❌ Paid ($29/mo, patent-pending) | — | Licensed install (coming) |
| Operator Bundle | ❌ Paid ($29/mo) | — | Licensed install (coming) |

---

## Manifest Change History

| Date | Change | Authorized by |
|------|--------|---------------|
| 2026-03-24 | Initial split: Transmission + Bonfire IP moved to private | Chip Ernst |
| 2026-03-24 | Agent911 moved to private | Chip Ernst |
| 2026-03-24 | Repo made public, ACME Freeware License added | Chip Ernst |
| 2026-03-24 | This manifest created | Soren Pagurus (PM) |
