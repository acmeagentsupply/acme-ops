# OCTriageUnit — Control Plane Trust Doctrine

> Status: READY | Audience: Internal + eventual open-source users
> Owner: Hendrik / GP-OPS | Date: 2026-02-28

---

## PURPOSE

Establish explicit trust posture for OCTriageUnit so the community:

- Understands exactly what the tool does
- Can verify it is safe
- Does not perceive hidden behavior
- Gains confidence in the OpenClaw ecosystem

This doctrine governs code, packaging, and messaging.

---

## TRUST PRINCIPLES (NON-NEGOTIABLE)

### 1. READ-ONLY BY DEFAULT

OCTriageUnit must never modify system state unless the operator explicitly opts in.

**Baseline guarantees:**
- No config mutation
- No service restarts
- No network writes (unless user-enabled in future SKUs)
- Proof bundle written only under user home

*Rationale: strong software assurance requires predictable, verifiable behavior.*

---

### 2. NO SECURITY THROUGH OBSCURITY

We do NOT rely on hidden behavior.

**Implications:**
- No hidden network calls
- No silent telemetry
- No opaque binary-only logic
- No "phone home" behavior

> Obscurity alone is not a reliable security control and should not be the trust model.

**Positioning:** OpenClaw tools are inspectable infrastructure, not black boxes.

---

### 3. SOURCE-VISIBLE FIRST

OCTriageUnit must ship with:
- Public source repo
- Reproducible build instructions
- Checksum verification
- Clear dependency list

**Community expectation:** Serious operators will not run opaque compiled tooling. We lean into that.

---

### 4. LATENT FEATURE DISCIPLINE

Future hooks are allowed but must be:
- Inert by default
- Clearly documented
- User-activated only
- Not network-active until enabled

**Hard rule:** If a feature would surprise a careful code reader → it does not ship.

---

### 5. PROOF-BUNDLE CULTURE

Every diagnostic claim must be backed by artifacts.

**OCTriageUnit standard output:**
- `bundle_summary.txt`
- `gateway_err_tail.txt`
- `launchctl snapshots`
- `doctor output`

> We don't assert — we prove.

---

### 6. OPERATOR-FIRST UX

The tool must remain:
- Fast
- Deterministic
- Safe under degraded systems
- Non-blocking

Timeout discipline stays mandatory.

---

## RELEASE HARDENING CHECKLIST (HENDRIK OWNERSHIP)

Before public release, verify:

- [ ] Script runs fully read-only
- [ ] No implicit network calls
- [ ] No background daemons installed
- [ ] No LaunchAgent writes
- [ ] Build reproducible from source
- [ ] SHA256 published
- [ ] LICENSE present
- [ ] SECURITY.md present
- [ ] README threat model included
- [ ] Sample proof bundle included

---

## STRATEGIC NOTE — Trust → Revenue Funnel

```
OCTriageUnit (free wedge)     →  visibility
Sentinel                      →  reliability (first paid attach)
Agent911                      →  recovery + control plane (revenue gravity)
```

**Do NOT contaminate the wedge with anything that smells proprietary too early.**

Free tool builds trust. Trust opens wallets. Sentinel closes the first deal.

---

## CODEX IMPLEMENTATION SCOPE

Repo structure target:

```
octriageunit/
├── README.md          (Safety Guarantees section mandatory)
├── LICENSE
├── SECURITY.md        (threat model, disclosure, build verification)
├── docs/
└── bin/
    └── control-plane-triage
```

*See codex task: "OCTriageUnit Trust Hardening Pass"*

---

*Last updated: 2026-02-28 | Source: Chip Ernst control plane trust doctrine*
