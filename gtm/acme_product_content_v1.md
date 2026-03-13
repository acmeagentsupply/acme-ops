# ACME Agent Supply Co. — Product Content Draft v1
OWNER: GP-GTM / GP-WEB
AUTHOR: Hendrik (GP-OPS)
DATE: 2026-02-26
STATUS: DRAFT — for review by Chip before publishing

Products in launch set: RadCheck · Sentinel · SphinxGate · Agent911 · (Lazarus TBD)

---

## 1. FEATURE MATRIX

### What Each Unit Does

| Capability                        | RadCheck | Sentinel | SphinxGate | Agent911 | Lazarus |
|-----------------------------------|:--------:|:--------:|:----------:|:--------:|:-------:|
| System reliability score (0–100)  |    ✓     |          |            |  reads   |         |
| 20+ automated checks              |    ✓     |          |            |          |         |
| Historical trend / velocity       |    ✓     |          |            |  reads   |         |
| Domain-weighted scoring           |    ✓     |          |            |          |         |
| Silent failure detection          |    ✓     |    ✓     |            |          |         |
| Drift detection                   |    ✓     |    ✓     |            |          |         |
| Compaction storm detection        |          |    ✓     |            |  reads   |         |
| Proof bundle generation           |    ✓     |    ✓     |            |          |         |
| Backward-compatible patch         |          |    ✓     |            |          |         |
| Model lane enforcement            |          |          |     ✓      |          |         |
| Interactive vs. background lanes  |          |          |     ✓      |          |         |
| Token accounting                  |          |          |     ✓      |          |         |
| Budget exhaustion protection      |          |          |     ✓      |          |         |
| Router visibility                 |          |          |     ✓      |          |         |
| Multi-provider fallback           |          |          |     ✓      |          |         |
| Single-pane reliability snapshot  |          |          |            |    ✓     |         |
| Gateway recovery playbooks        |          |          |            |    ✓     |         |
| Config rehydration (known-good)   |          |          |            |    ✓     |         |
| launchd repair + reload           |          |          |            |    ✓     |         |
| Backup readiness scoring          |          |          |            |          |    ✓    |
| Restore drill validation          |          |          |            |          |    ✓    |
| Resurrection plan generation      |          |          |            |          |    ✓    |
| GDrive / snapshot verification    |          |          |            |          |    ✓    |

### What Each Unit Does NOT Do (honest limits)
| Limit                                      | RadCheck | Sentinel | SphinxGate | Agent911 | Lazarus |
|--------------------------------------------|:--------:|:--------:|:----------:|:--------:|:-------:|
| Autonomous healing (v1)                    |    ✗     |    ✗     |     ✗      |    ✗     |    ✗    |
| Modify openclaw.json                       |    ✗     |    ✗     |     ✗      |    ✗     |    ✗    |
| Restart gateway without authorization      |    ✗     |    ✗     |     ✗      |    ✗     |    ✗    |
| Real-time context budget monitoring        |    ✗     |    ✗     |     ✗      |    ✗     |    ✗    |
| Replace human judgment on recovery         |    ✗     |    ✗     |     ✗      |    ✗     |    ✗    |

---

## 2. VALUE ANCHORS
### Real numbers from production ops on an OpenClaw-class stack.
### Use these verbatim or as templates for site copy.

---

**RadCheck**

> "Caught a 36x compaction acceleration before it took the gateway down."
> Score dropped 74→51 in 2.5 hours (−8.6 pts/hr). That's 2.5 hours of warning before the system became unresponsive.

> "First scan on a 'working' system scored 45/100 HIGH RISK."
> 20+ checks ran in 7 seconds. Found issues the team didn't know existed. No config modified.

> "Score went from 45 to 74 after fixes — same system, same day."
> Risk velocity shows you're moving in the right direction, not just that something happened.

---

**Sentinel**

> "Detected back-to-back 10-minute compaction timeout storms."
> Two consecutive 599.9s and 600.0s timeouts. 20 minutes of unresponsive agent. Sentinel saw it coming; the team didn't.

> "Caught a 101-minute watchdog silence gap."
> Agent was 'running.' Heartbeat log disagreed. Sentinel flagged it. That's the difference between a ghost and a guardian.

> "COMPACTION_STORM_ALERT fires with 30-minute cooldown. One ping, not a flood."
> Because the last thing you need when your system is on fire is a notification storm.

---

**SphinxGate**

> "Four model providers in the fallback chain. Primary exhaustion → automatic reroute in <1 second."
> Background agents eat budget. SphinxGate keeps them in their lane so your interactive sessions stay fast.

> "Token spend spiked overnight. Background lane was uncapped."
> After SphinxGate: background runs capped, interactive priority enforced, overnight bill normalized.

> "One policy file. Every routing decision auditable."
> sphinxgate_policy.json + model_events.log. You know which model handled every turn and why.

---

**Agent911**

> "8ms to a full reliability snapshot. Stability score, top risks, protection state, backup readiness, model health, compaction risk — all in one view."
> When something goes wrong at 2 AM, you don't have time to grep five log files. Agent911 does it in 8ms.

> "Gateway dead. Probe failing. Port up. Loop frozen."
> Agent911 surfaces the exact pattern — port-up/probe-fail — that preceded our overnight outage. Know the signature before it becomes an incident.

> "Recovery playbook: launchd repair → config rehydration → controlled restart."
> Not 'have you tried turning it off and on again.' A deterministic sequence with exit codes and rollback at every step.

---

**Lazarus** *(launch TBD)*

> "First run scored 75/100 MODERATE on a system the team thought was fully backed up."
> Missing restore validation. No verified recovery path. Lazarus found it in 10.8 seconds.

> "Restore dry-run: ALL_INVARIANTS_PASSED. Exit 0."
> You don't know you can recover until you've tried. Lazarus runs the drill so you're not practicing during the incident.

> "13 checks. 0–100 resurrection score. Modes: scan, plan, generate, validate."
> Built for real-world OpenClaw-class stacks that actually go down.

---

## 3. SYMPTOM-TO-PRODUCT MAPPING
### Extend the "Human Resources" live feed concept into pricing rationale.
### Format: operator symptom → recommended unit → why it fits

---

| Operator says...                                    | Recommended Unit | The fit                                                                 |
|-----------------------------------------------------|-----------------|-------------------------------------------------------------------------|
| "Cron ran twice. I didn't ask it to."               | Sentinel        | Lockfile protection, duplicate run prevention, proof of what ran        |
| "Token spend spiked overnight."                     | SphinxGate      | Background vs interactive lane caps, token accounting, audit log        |
| "It says 'done' but nothing changed."               | Sentinel + RadCheck | Proof bundle generation; score penalizes silent success            |
| "The agent crashed and I don't know why."           | RadCheck        | 20+ checks, drift detection, compaction pattern recognition             |
| "We had an incident. What's the blast radius?"      | Agent911        | 8ms snapshot: score, top risks, protection state, backup, model, compaction |
| "We lost our agent's memory."                       | Lazarus         | Backup readiness score, restore validation, resurrection planning        |
| "My background agent is eating all our budget."     | SphinxGate      | Lane enforcement, budget caps, model-level token accounting             |
| "I don't know if we can recover from this."         | Lazarus         | Dry-run restore, invariant checks, scored recovery confidence           |
| "The system was fine yesterday."                    | RadCheck        | Velocity: score dropped −8.6 pts/hr; was DEGRADING before you noticed  |
| "We hit a compaction storm and lost 20 minutes."    | Sentinel        | Storm detection, cron wake alert, early session hygiene nudge           |
| "Which model is handling my production traffic?"    | SphinxGate      | Router visibility, per-turn provider log, lane audit trail              |
| "My agent went silent for an hour."                 | Sentinel        | Silence sentinel: last message age, silence_warn flag per cycle         |
| "Gateway is up but nothing's working."              | Agent911        | Port-up/probe-fail pattern; frozen event loop detection; recovery path  |
| "Is our config backed up?"                          | Lazarus         | GDrive snapshot verification, repo sync check, manifest integrity       |
| "Something changed and I don't know what."          | RadCheck        | Finding delta between runs, domain subscores, enriched NDJSON findings  |

---

## 4. PRICING FRAME (draft rationale — not final numbers)

### The tier story
These products have a natural stacking logic:

- **START HERE** — RadCheck + Sentinel
  You need to know what's broken before you can fix it. RadCheck finds it. Sentinel stops it from going quiet.

- **ADD GATE CONTROL** — + SphinxGate
  Once your agents are reliable, protect the budget. Background agents will eat your lunch without lane enforcement.

- **ADD RECOVERY** — + Agent911
  When something goes wrong (and it will), you want a playbook, not a grep session.

- **ADD RESURRECTION** — + Lazarus *(if launching)*
  You don't know you can recover until you've tried. Lazarus scores your backup readiness before the incident.

### Pricing copy angles
- Not priced by seat — priced by what going down costs you
- "The 36x compaction storm cost 20 minutes of agent downtime and a full reboot. RadCheck + Sentinel together cost less than that hour."
- Operators self-select: if you're asking "which model handled that turn?" — you need SphinxGate. If you're asking "can we recover?" — you need Lazarus.

---

## 5. SHORT PRODUCT DESCRIPTIONS (for cards, meta, taglines)

**RadCheck**
Tagline: *"You can't fix what you can't see."*
One-liner: Automated reliability scoring for agent stacks. 20+ checks. 0–100 score. Risk trend in every run.

**Sentinel**
Tagline: *"When agents quietly go off-script, Sentinel notices first."*
One-liner: Containment for real workloads. Silent failure detection, drift guardrails, proof bundles — without touching your config.

**SphinxGate**
Tagline: *"Keep background runs from eating the whole budget."*
One-liner: Token discipline and model lane enforcement for OpenClaw-class stacks. One policy file. Full router visibility.

**Agent911**
Tagline: *"When the system goes feral, Agent911 brings it back — carefully."*
One-liner: Deterministic recovery for agent stacks. Stability snapshot in 8ms. Gateway playbooks, config rehydration, controlled self-healing.

**Lazarus** *(TBD)*
Tagline: *"You don't know you can recover until you've tried."*
One-liner: Backup readiness scoring and resurrection planning for agent runtimes. Runs the drill before the incident finds you.

---

## NOTES FOR GP-WEB

1. The "Human Resources" live feed on the site is the strongest UX concept. Consider making the symptom list above the actual feed copy (rotating or scrollable).
2. Real numbers > abstract promises. The 36x compaction acceleration, the 8ms snapshot, the 101-minute silence gap — these came from production. Use them.
3. The "Picks. Shovels. Guardrails." tagline is perfect — resist the urge to polish it into something corporate.
4. Watchdog: not in launch set per Chip. Could be bundled as infrastructure-layer (included with Sentinel) rather than sold standalone.
5. All copy here reflects real behavior of real code on a real OpenClaw stack. Don't fabricate numbers; they'll get audited.

---
*Draft by Hendrik (GP-OPS) — built the tools, wrote the copy. Ask GP-GTM to pressure-test pricing frame.*
