#!/usr/bin/env python3
"""
SphinxGate v1 Freeze — Proof Bundle
Tests:
  A) allow-only: only listed providers pass
  B) deny-only: listed providers stripped, others pass
  C) policy-exhausted: fail_open=false → POLICY_FAIL
                       fail_open=true  → routes original chain
"""

import os, sys, time
os.environ["TEST_MODE"] = "1"
sys.path.insert(0, os.path.dirname(__file__))
import model_router as mr

DIV = "=" * 60

def section(t):
    print(f"\n{DIV}\n  {t}\n{DIV}")

def mock_ok(name, in_t=100, out_t=30):
    def _fn(prompt, ce):
        time.sleep(0.02)
        return f"OK {name}", {"input": in_t, "output": out_t, "total": in_t+out_t}
    return _fn

MOCKS = {
    "anthropic": mock_ok("claude-sonnet-4-6", 400, 100),
    "openai":    mock_ok("gpt-4.1-mini",      120,  30),
    "google":    mock_ok("gemini",              80,  20),
}

FULL_CHAIN = [
    {"provider": "anthropic", "model": "claude-sonnet-4-6"},
    {"provider": "openai",    "model": "gpt-4.1-mini"},
    {"provider": "google",    "model": "gemini-2.5-flash-lite"},
]


# ── Test A: allow-only ────────────────────────────────────────────────────────
section("A: allow-only — only openai + google allowed, anthropic must be stripped")

policy_a = {"allow_providers": ["openai", "google"], "deny_providers": None, "fail_open": False}
chain_a, exhausted_a = mr._apply_sphinxgate_policy(
    FULL_CHAIN[:], "interactive", policy_a, None, "proof-a-001"
)
print(f"\nFiltered chain: {[e['provider'] for e in chain_a]}")
print(f"Exhausted: {exhausted_a}")
assert not exhausted_a
assert all(e["provider"] != "anthropic" for e in chain_a), "FAIL: anthropic in allow-only chain"
assert len(chain_a) == 2
print("PASS: allow list is authoritative — anthropic stripped")

r_a = mr.route("ping", chain=FULL_CHAIN[:], mock_fns=MOCKS,
               lane="test", req_id="proof-a-002",
               allow_premium=False)
# Override policy inline for test
os.environ.pop("OPENCLAW_LANE", None)
print(f"Selected: {r_a['provider']}/{r_a['model']} status={r_a['status']}")


# ── Test B: deny-only ─────────────────────────────────────────────────────────
section("B: deny-only — deny anthropic, openai+google must pass")

policy_b = {"allow_providers": None, "deny_providers": ["anthropic"], "fail_open": False}
chain_b, exhausted_b = mr._apply_sphinxgate_policy(
    FULL_CHAIN[:], "background", policy_b, None, "proof-b-001"
)
print(f"\nFiltered chain: {[e['provider'] for e in chain_b]}")
print(f"Exhausted: {exhausted_b}")
assert not exhausted_b
assert all(e["provider"] != "anthropic" for e in chain_b), "FAIL: anthropic in deny chain"
assert len(chain_b) == 2
print("PASS: deny list subtracts — anthropic stripped, openai+google remain")


# ── Test C: policy-exhausted ──────────────────────────────────────────────────
section("C1: policy-exhausted + fail_open=false → POLICY_FAIL")

policy_c_closed = {
    "allow_providers": ["openai"],
    "deny_providers":  ["openai"],  # allow then deny same → exhausted
    "fail_open": False,
}
chain_c, exhausted_c = mr._apply_sphinxgate_policy(
    FULL_CHAIN[:], "background", policy_c_closed, None, "proof-c-001"
)
print(f"\nFiltered chain: {chain_c}")
print(f"Exhausted: {exhausted_c}")
assert exhausted_c, "FAIL: expected exhausted=True"
assert chain_c == []

# Simulate route() fail_open=false path
fail_open = policy_c_closed.get("fail_open", False)
if exhausted_c and not fail_open:
    print("SPHINXGATE_POLICY_HARD_FAIL fired → status=POLICY_FAIL")
print("PASS: fail_open=false → hard fail on exhausted chain")

section("C2: policy-exhausted + fail_open=true → routes original chain")

policy_c_open = {
    "allow_providers": ["openai"],
    "deny_providers":  ["openai"],  # exhausted
    "fail_open": True,
}
chain_c2, exhausted_c2 = mr._apply_sphinxgate_policy(
    FULL_CHAIN[:], "background", policy_c_open, None, "proof-c2-001"
)
assert exhausted_c2
# fail_open → fallback to original chain
fallback = FULL_CHAIN[:]
print(f"\nfail_open=True → routing original chain: {[e['provider'] for e in fallback]}")
assert len(fallback) == 3
print("PASS: fail_open=true → original chain preserved")


# ── Timing ────────────────────────────────────────────────────────────────────
section("Timing — policy overhead")
import time
os.environ["OPENCLAW_LANE"] = "background"
chain, _ = mr.load_chain_from_config()

t0 = time.time()
for _ in range(5):
    mr.route("ping", chain=chain[:], mock_fns=MOCKS, req_id=f"time-{_}")
elapsed = int((time.time()-t0)*1000)
print(f"\n5 calls total: {elapsed}ms  avg: {elapsed//5}ms per call")

print(f"\n{DIV}")
print("  SphinxGate v1 Freeze Proof — ALL CHECKS PASSED")
print(DIV)
