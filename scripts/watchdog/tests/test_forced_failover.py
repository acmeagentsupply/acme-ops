#!/usr/bin/env python3
"""
Acceptance tests for model_router.py — Forced Failover (P1.1)

TEST A — Forced timeout:  primary sleeps beyond timeout → TIMEOUT_ABORT + FAILOVER_TRIGGERED + fallback runs
TEST B — Healthy primary: fast response → no failover lines emitted
TEST C — Full chain fail: all providers error → FAILOVER_EXHAUSTED
"""

import os
import sys
import time
import threading
import tempfile

# B1: prevent test runs from writing to runtime model_state.json
os.environ["TEST_MODE"] = "1"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from model_router import route

TIMEOUT_S = 3   # short for test speed; production uses 90s
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

failures = []

def assert_ok(name, condition, msg=""):
    if condition:
        print(f"  [{PASS}] {name}")
    else:
        print(f"  [{FAIL}] {name}  {msg}")
        failures.append(name)

def count_lines(log_file, keyword):
    try:
        with open(log_file) as f:
            return sum(1 for l in f if keyword in l)
    except Exception:
        return 0


# ── Mock provider factories ───────────────────────────────────────────────────

def slow_provider(sleep_s):
    """Provider that sleeps sleep_s then returns (simulates stall)."""
    def _call(prompt, cancel_event):
        deadline = time.time() + sleep_s
        while time.time() < deadline:
            if cancel_event.is_set():
                raise InterruptedError("cancelled")
            time.sleep(0.1)
        return "slow-response"
    return _call

def fast_provider(reply="fast-ok"):
    """Provider that returns immediately."""
    def _call(prompt, cancel_event):
        return reply
    return _call

def error_provider(msg="provider-down"):
    """Provider that always raises."""
    def _call(prompt, cancel_event):
        raise Exception(msg)
    return _call


# ── TEST A — Forced timeout ───────────────────────────────────────────────────
print("\n=== TEST A: forced timeout (primary stalls, fallback fires) ===")

log_a = tempfile.mktemp(suffix="_test_a.log")
chain_a = [
    {"provider": "anthropic", "model": "claude-sonnet-4-6"},
    {"provider": "openai",    "model": "gpt-4.1-mini"},
]
mock_a = {
    "anthropic": slow_provider(sleep_s=30),    # far longer than TIMEOUT_S=3
    "openai":    fast_provider("fallback-ok"),
}

t_start = time.time()
result_a = route(
    "test", chain=chain_a, timeout_s=TIMEOUT_S,
    req_id="req-test-a", log_file=log_a, mock_fns=mock_a,
)
elapsed_a = time.time() - t_start

print(f"  elapsed: {elapsed_a:.1f}s  (should be ~{TIMEOUT_S}s + tiny fallback)")
print(f"  result:  {result_a}")

assert_ok("A1: TIMEOUT_ABORT logged",
          count_lines(log_a, "MODEL_TIMEOUT_ABORT") == 1)
assert_ok("A2: FAILOVER_TRIGGERED logged",
          count_lines(log_a, "MODEL_FAILOVER_TRIGGERED") == 1)
assert_ok("A3: fallback provider ran (status=failover_ok)",
          result_a.get("status") == "failover_ok")
assert_ok("A4: correct fallback provider",
          result_a.get("provider") == "openai")
assert_ok("A5: latency bounded (< timeout + 5s buffer)",
          elapsed_a < TIMEOUT_S + 5)
assert_ok("A6: NO FAILOVER_SUCCESS logged (single success line check)",
          count_lines(log_a, "MODEL_FAILOVER_SUCCESS") == 1)

# Dedup check: route again with same req_id — not applicable (no dedup at router level,
# dedup is in stall_detector; but verify no duplicate ABORT for same run)
print(f"  log contents:")
with open(log_a) as f:
    for line in f:
        print(f"    {line.rstrip()}")


# ── TEST B — Healthy primary ──────────────────────────────────────────────────
print("\n=== TEST B: healthy primary (no failover) ===")

log_b = tempfile.mktemp(suffix="_test_b.log")
chain_b = [
    {"provider": "anthropic", "model": "claude-sonnet-4-6"},
    {"provider": "openai",    "model": "gpt-4.1-mini"},
]
mock_b = {
    "anthropic": fast_provider("primary-ok"),
    "openai":    fast_provider("fallback-ok"),
}

result_b = route(
    "test", chain=chain_b, timeout_s=TIMEOUT_S,
    req_id="req-test-b", log_file=log_b, mock_fns=mock_b,
)

print(f"  result:  {result_b}")

assert_ok("B1: primary provider used",
          result_b.get("provider") == "anthropic")
assert_ok("B2: status=ok",
          result_b.get("status") == "ok")
assert_ok("B3: NO TIMEOUT_ABORT lines",
          count_lines(log_b, "MODEL_TIMEOUT_ABORT") == 0)
assert_ok("B4: NO FAILOVER_TRIGGERED lines",
          count_lines(log_b, "MODEL_FAILOVER_TRIGGERED") == 0)
assert_ok("B5: NO FAILOVER_SUCCESS lines",
          count_lines(log_b, "MODEL_FAILOVER_SUCCESS") == 0)

print(f"  log contents: (should be empty)")
try:
    with open(log_b) as f:
        content = f.read().strip()
    print(f"    '{content}'" if content else "    (empty — correct)")
except FileNotFoundError:
    print("    (no log file — correct, nothing to log)")


# ── TEST C — Full chain exhausted ────────────────────────────────────────────
print("\n=== TEST C: full chain failure (all providers error) ===")

log_c = tempfile.mktemp(suffix="_test_c.log")
chain_c = [
    {"provider": "anthropic",  "model": "claude-sonnet-4-6"},
    {"provider": "openai",     "model": "gpt-4.1-mini"},
    {"provider": "google",     "model": "gemini-2.5-flash-lite"},
]
mock_c = {
    "anthropic": error_provider("anthropic-down"),
    "openai":    error_provider("openai-down"),
    "google":    error_provider("google-down"),
}

result_c = route(
    "test", chain=chain_c, timeout_s=TIMEOUT_S,
    req_id="req-test-c", log_file=log_c, mock_fns=mock_c,
)

print(f"  result:  {result_c}")

assert_ok("C1: status=failover_exhausted",
          result_c.get("status") == "failover_exhausted")
assert_ok("C2: text=None",
          result_c.get("text") is None)
assert_ok("C3: FAILOVER_EXHAUSTED logged",
          count_lines(log_c, "MODEL_FAILOVER_EXHAUSTED") == 1)
assert_ok("C4: FAILOVER_TRIGGERED logged for each transition (2 transitions)",
          count_lines(log_c, "MODEL_FAILOVER_TRIGGERED") == 2)
assert_ok("C5: 3 PROVIDER_ERROR lines",
          count_lines(log_c, "MODEL_PROVIDER_ERROR") == 3)

print(f"  log contents:")
with open(log_c) as f:
    for line in f:
        print(f"    {line.rstrip()}")


# ── Summary ───────────────────────────────────────────────────────────────────
print("\n" + "="*60)
if failures:
    print(f"RESULT: {len(failures)} FAILED: {failures}")
    sys.exit(1)
else:
    print(f"RESULT: ALL TESTS PASSED")
    sys.exit(0)
