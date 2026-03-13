#!/usr/bin/env python3
"""
SphinxGate Proof Bundle — runs all required proof cases.
Uses mock providers — no real API calls, no token burn.

Sections:
  A) Config verification
  B) Background path — Claude blocked, gpt-4.1-mini selected
  C) Interactive path — Claude allowed as primary
  D) Failover visibility — FAIL lines per attempt, OK on winner
  E) tokens-status output
"""

import os, sys, json, time, uuid, tempfile
os.environ["TEST_MODE"] = "1"  # block model_state.json writes

sys.path.insert(0, os.path.dirname(__file__))
import model_router as mr

DIVIDER = "=" * 60

def section(title):
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


# ── Mocks ─────────────────────────────────────────────────────────────────────
# Each mock returns (text, usage) — matching updated provider caller contract

def mock_ok(name, in_t, out_t):
    def _fn(prompt, cancel_event):
        time.sleep(0.05)
        return f"OK from {name}", {"input": in_t, "output": out_t, "total": in_t + out_t}
    return _fn

def mock_fail(name):
    def _fn(prompt, cancel_event):
        time.sleep(0.05)
        raise mr.ProviderError(f"Simulated failure from {name}")
    return _fn


# ── A: Config verification ────────────────────────────────────────────────────
section("A: Config verification")

interactive_chain, src = mr.load_chain_from_config()
bg_chain, bg_src       = mr.load_background_chain()

print(f"Interactive chain (source={src}):")
for e in interactive_chain:
    claude = " <-- CLAUDE" if mr._is_claude(e) else ""
    print(f"  {e['provider']}/{e['model']}{claude}")

print(f"\nBackground chain (source={bg_src}):")
for e in bg_chain:
    claude = " <-- CLAUDE (should not appear)" if mr._is_claude(e) else ""
    print(f"  {e['provider']}/{e['model']}{claude}")

claude_in_interactive = any(mr._is_claude(e) for e in interactive_chain)
claude_in_background  = any(mr._is_claude(e) for e in bg_chain)
print(f"\nClaude in interactive chain: {claude_in_interactive}  (expected: True)")
print(f"Claude in background chain:  {claude_in_background}  (expected: False)")
assert claude_in_interactive, "FAIL: Claude missing from interactive chain"
assert not claude_in_background, "FAIL: Claude present in background chain"
print("PASS: Config verified")


# ── B: Background path — Claude blocked ───────────────────────────────────────
section("B: Background path — lane=background, Claude MUST be blocked")

# Use interactive chain (includes Claude) — guardrail must strip it
mocks_b = {
    "openai":    mock_ok("gpt-4.1-mini", 120, 30),
    "google":    mock_ok("gemini",        80, 20),
}

result_b = mr.route(
    "Test background lane",
    chain=interactive_chain[:],   # explicitly pass chain WITH Claude
    mock_fns=mocks_b,
    lane="background",
    allow_premium=False,
    req_id="proof-bg-001",
)

print(f"\nSelected provider : {result_b['provider']}")
print(f"Selected model    : {result_b['model']}")
print(f"Status            : {result_b['status']}")
print(f"Usage             : {result_b['usage']}")
print(f"Text              : {result_b['text']}")

assert result_b["provider"] != "anthropic", \
    f"FAIL: Claude was selected in background lane! provider={result_b['provider']}"
assert result_b["status"].startswith("OK"), \
    f"FAIL: Expected OK status, got {result_b['status']}"
print("\nPASS: Background lane did NOT select Claude")


# ── C: Interactive path — Claude is primary ───────────────────────────────────
section("C: Interactive path — no lane set, Claude is primary")

mocks_c = {
    "anthropic": mock_ok("claude-sonnet-4-6", 500, 150),
    "openai":    mock_ok("gpt-4.1-mini",      120,  30),
}

# Unset OPENCLAW_LANE so it defaults to interactive
os.environ.pop("OPENCLAW_LANE", None)

result_c = mr.route(
    "Test interactive lane",
    chain=interactive_chain[:],
    mock_fns=mocks_c,
    req_id="proof-ia-001",
)

print(f"\nSelected provider : {result_c['provider']}")
print(f"Selected model    : {result_c['model']}")
print(f"Status            : {result_c['status']}")
print(f"Usage             : {result_c['usage']}")
print(f"Text              : {result_c['text']}")

assert result_c["provider"] == "anthropic", \
    f"FAIL: Expected Claude as primary, got {result_c['provider']}"
assert result_c["status"] == "OK", \
    f"FAIL: Expected OK status, got {result_c['status']}"
print("\nPASS: Interactive lane selected Claude as primary")


# ── D: Failover visibility — FAIL lines logged per failed attempt ─────────────
section("D: Failover visibility — FAIL lines per attempt, OK on winner")

with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as tmp:
    tmp_tokens = tmp.name

# Monkey-patch TOKENS_LOG to temp file for this test
original_tokens_log = mr.TOKENS_LOG
mr.TOKENS_LOG = tmp_tokens

mocks_d = {
    "anthropic": mock_fail("anthropic"),     # FAIL
    "openai":    mock_fail("openai"),         # FAIL
    "google":    mock_ok("gemini", 80, 20),  # OK
}

result_d = mr.route(
    "Test failover visibility",
    chain=interactive_chain[:],
    mock_fns=mocks_d,
    lane="interactive",
    req_id="proof-fo-001",
)

mr.TOKENS_LOG = original_tokens_log  # restore

print(f"\nFinal provider: {result_d['provider']} / status: {result_d['status']}")
print(f"\ntokens.log entries for this req:")
with open(tmp_tokens) as f:
    lines = f.readlines()
for ln in lines:
    print(" ", ln.strip())

fail_lines = [l for l in lines if ",FAIL," in l]
ok_lines   = [l for l in lines if ",OK"   in l]
print(f"\nFAIL lines: {len(fail_lines)}  (expected: 2)")
print(f"OK lines  : {len(ok_lines)}    (expected: 1)")
assert len(fail_lines) == 2, f"FAIL: Expected 2 FAIL lines, got {len(fail_lines)}"
assert len(ok_lines)   == 1, f"FAIL: Expected 1 OK line, got {len(ok_lines)}"
os.unlink(tmp_tokens)
print("\nPASS: Failover visibility confirmed — FAIL lines per attempt")


# ── E: tokens-status ──────────────────────────────────────────────────────────
section("E: tokens-status output")
print()
mr.cmd_tokens_status()

print(f"\n{DIVIDER}")
print("  SphinxGate Proof Bundle — ALL CHECKS PASSED")
print(DIVIDER)
