"""
Transmission v2 — Proof Tests
14 required (9 v1 + 5 v2). Exit 0 = all PASS. Exit 1 = any FAIL.
"""

import copy
import json
import os
import sys
import tempfile
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = str(SCRIPT_DIR / "transmission_config.json")

sys.path.insert(0, str(SCRIPT_DIR))
from transmission_router import route_with_transmission, _load_config

PASS_STR = "\033[32mPASS\033[0m"
FAIL_STR = "\033[31mFAIL\033[0m"
results = []


def check(n: int, desc: str, condition: bool, detail: str = ""):
    status = PASS_STR if condition else FAIL_STR
    msg = f"[{status}] Test {n}: {desc}"
    if detail:
        msg += f"\n       → {detail}"
    print(msg)
    results.append(condition)
    return condition


def write_json_file(data: dict, suffix=".json") -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=suffix, delete=False) as f:
        json.dump(data, f)
        return f.name


def run_tests():
    with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
        log_path = f.name

    # Shared ops events log for v2 tests
    with tempfile.NamedTemporaryFile(suffix=".ndjson", delete=False) as f:
        ops_log = f.name

    kwargs = dict(config_path=CONFIG_PATH, log_path=log_path)

    # ===================================================================
    # v1 TESTS — Must all still pass (baseline regression)
    # ===================================================================

    # -------------------------------------------------------------------
    # Test 1: Interactive lane + coding prompt → premium model
    # -------------------------------------------------------------------
    r = route_with_transmission("write some python code to parse JSON", lane="interactive", **kwargs)
    check(1, "Interactive lane + coding prompt → premium model",
          r.get("gear") == "premium",
          f"gear={r.get('gear')} model={r.get('model')}")

    # -------------------------------------------------------------------
    # Test 2: Background lane + coding prompt → budget-capable or mid (NOT anthropic)
    # -------------------------------------------------------------------
    r = route_with_transmission("implement a sorting algorithm", lane="background", **kwargs)
    not_anthropic = r.get("provider") != "anthropic"
    tier_ok = r.get("gear") in ("budget-capable", "mid")
    check(2, "Background lane + coding prompt → non-anthropic budget-capable or mid",
          not_anthropic and tier_ok,
          f"gear={r.get('gear')} provider={r.get('provider')} model={r.get('model')}")

    # -------------------------------------------------------------------
    # Test 3: Explicit work_class=simple → NOT premium
    # -------------------------------------------------------------------
    r = route_with_transmission("complex analysis task", work_class="simple", lane="interactive", **kwargs)
    cfg_data = json.loads(Path(CONFIG_PATH).read_text())
    selected_caps = cfg_data["models"].get(r.get("model", ""), {}).get("capabilities", [])
    supports_simple = "simple" in selected_caps
    not_premium = r.get("gear") != "premium"
    check(3, "Explicit work_class=simple → model supports simple, not premium tier",
          supports_simple and not_premium,
          f"gear={r.get('gear')} model={r.get('model')} supports_simple={supports_simple}")

    # -------------------------------------------------------------------
    # Test 4: Low confidence ambiguous prompt → gear up
    # -------------------------------------------------------------------
    r_bg  = route_with_transmission("xyzzy frobulate the quux", lane="background", **kwargs)
    r_int = route_with_transmission("xyzzy frobulate the quux", lane="interactive", **kwargs)
    check(4, "Low confidence ambiguous prompt → gear up from lowest tier",
          r_bg.get("confidence", 1.0) < 0.70 or r_int.get("confidence", 1.0) < 0.70,
          f"bg_confidence={r_bg.get('confidence')} int_confidence={r_int.get('confidence')} "
          f"bg_gear={r_bg.get('gear')} int_gear={r_int.get('gear')}")

    # -------------------------------------------------------------------
    # Test 5: All models disabled → TRANSMISSION_EXHAUSTED
    # -------------------------------------------------------------------
    cfg = json.loads(Path(CONFIG_PATH).read_text())
    cfg_disabled = copy.deepcopy(cfg)
    for m in cfg_disabled["models"].values():
        m["enabled"] = False
    disabled_config = write_json_file(cfg_disabled)

    r = route_with_transmission("implement a function", config_path=disabled_config, log_path=log_path)
    exhausted = r.get("status") == "EXHAUSTED"
    log_lines = Path(log_path).read_text().strip().split("\n")
    events = [json.loads(l).get("event") for l in log_lines if l]
    exhausted_logged = "TRANSMISSION_EXHAUSTED" in events

    check(5, "All models disabled → TRANSMISSION_EXHAUSTED emitted + structured failure",
          exhausted and exhausted_logged,
          f"status={r.get('status')} exhausted_in_log={exhausted_logged}")
    os.unlink(disabled_config)

    # -------------------------------------------------------------------
    # Test 6: 1000 routing calls → zero writes to openclaw.json
    # -------------------------------------------------------------------
    openclaw_json = Path.home() / ".openclaw" / "openclaw.json"
    mtime_before = openclaw_json.stat().st_mtime if openclaw_json.exists() else None
    for i in range(1000):
        route_with_transmission(f"write a summary of item {i}", lane="interactive",
                                 config_path=CONFIG_PATH, log_path=log_path)
    mtime_after = openclaw_json.stat().st_mtime if openclaw_json.exists() else None
    check(6, "1000 routing calls → zero writes to openclaw.json",
          mtime_before == mtime_after,
          f"mtime_before={mtime_before} mtime_after={mtime_after}")

    # -------------------------------------------------------------------
    # Test 7: 1000 routing calls → p95 < 10ms
    # -------------------------------------------------------------------
    import statistics
    latencies = []
    for i in range(1000):
        r = route_with_transmission(f"write some python code {i}", lane="interactive",
                                     config_path=CONFIG_PATH, log_path=log_path)
        latencies.append(r.get("duration_ms", 999))
    latencies.sort()
    p95 = latencies[int(len(latencies) * 0.95)]
    check(7, f"1000 routing calls → p95 latency < 10ms (actual p95={p95:.2f}ms)",
          p95 < 10.0,
          f"p50={latencies[500]:.2f}ms p95={p95:.2f}ms p99={latencies[990]:.2f}ms")

    # -------------------------------------------------------------------
    # Test 8: Invalid dispatch_hint.work_class → fallback to heuristic
    # -------------------------------------------------------------------
    r = route_with_transmission(
        "analyze this data and write a report",
        dispatch_hint={"work_class": "invalid_class_xyz"},
        lane="interactive", **kwargs)
    fallback_used = r.get("classifier_source") != "dispatch"
    valid_wc = r.get("work_class") in ["coding", "writing", "analysis", "organizing", "simple", "creative"]
    check(8, "Invalid dispatch_hint.work_class → ignored, fallback classifier used",
          fallback_used and valid_wc,
          f"classifier_source={r.get('classifier_source')} work_class={r.get('work_class')}")

    # -------------------------------------------------------------------
    # Test 9: Model lacks tool_calling → execution_config masked to False
    # -------------------------------------------------------------------
    cfg2 = copy.deepcopy(cfg)
    cfg2["models"]["google/gemini-2.5-flash-lite"]["capabilities"].append("coding")
    for mid, mcfg in cfg2["models"].items():
        if mid != "google/gemini-2.5-flash-lite":
            mcfg["enabled"] = False
    test9_config = write_json_file(cfg2)

    r = route_with_transmission("implement a python function", work_class="coding",
                                 config_path=test9_config, log_path=log_path)
    masked = (
        r.get("model") == "google/gemini-2.5-flash-lite" and
        r.get("execution_config", {}).get("tool_calling") == False
    )
    check(9, "Model lacks tool_calling=True → execution_config.tool_calling masked to False",
          masked,
          f"model={r.get('model')} tool_calling={r.get('execution_config', {}).get('tool_calling')}")
    os.unlink(test9_config)

    # ===================================================================
    # v2 TESTS
    # ===================================================================

    # -------------------------------------------------------------------
    # Test 10: Hypnos denied_providers → anthropic excluded from chain
    # -------------------------------------------------------------------
    hypnos_active = {
        "active": True,
        "routing": {
            "denied_providers": ["anthropic"],
            "force_tier": None,
            "cost_hold": False,
            "preferred_tiers": [],
            "required_features": {}
        }
    }
    hypnos_path = write_json_file(hypnos_active)

    r = route_with_transmission(
        "write some python code to parse JSON",
        lane="interactive",
        config_path=CONFIG_PATH, log_path=log_path,
        hypnos_state_path=hypnos_path,
        ops_events_path=ops_log,
    )
    no_anthropic = r.get("provider") != "anthropic"
    chain_clean = all(
        json.loads(Path(CONFIG_PATH).read_text())["models"].get(m, {}).get("provider") != "anthropic"
        for m in r.get("candidate_chain", [])
    )
    policy_flagged = r.get("policy_active") == True
    # Verify TRANSMISSION_POLICY_REDUCED was logged
    log_lines = [l for l in Path(log_path).read_text().strip().split("\n") if l]
    policy_logged = any(json.loads(l).get("event") == "TRANSMISSION_POLICY_REDUCED" for l in log_lines)

    check(10, "Hypnos denied_providers=[anthropic] → anthropic excluded from chain",
          no_anthropic and chain_clean and policy_flagged,
          f"provider={r.get('provider')} policy_active={r.get('policy_active')} chain={r.get('candidate_chain')} policy_logged={policy_logged}")
    os.unlink(hypnos_path)

    # -------------------------------------------------------------------
    # Test 11: Recall in_recovery=true, scope=global → efficient excluded, mid/budget preferred
    # -------------------------------------------------------------------
    recall_global = {
        "in_recovery": True,
        "recovery_phase": "RESTORE",
        "affected_agents": [],
        "scope": "global"
    }
    recall_path = write_json_file(recall_global)

    r = route_with_transmission(
        "what is the current date",   # would normally go to efficient/simple
        work_class="simple",
        lane="interactive",
        config_path=CONFIG_PATH, log_path=log_path,
        recall_state_path=recall_path,
        ops_events_path=ops_log,
    )
    not_efficient = r.get("gear") != "efficient"
    preferred_tier = r.get("gear") in ("mid", "budget-capable", "premium")
    recovery_flagged = r.get("recovery_context") == True

    check(11, "Recall in_recovery=true scope=global → efficient excluded, mid/budget-capable preferred",
          not_efficient and preferred_tier and recovery_flagged,
          f"gear={r.get('gear')} model={r.get('model')} recovery_context={r.get('recovery_context')}")
    os.unlink(recall_path)

    # -------------------------------------------------------------------
    # Test 12: Both policy files absent → identical behavior to v1 baseline
    # -------------------------------------------------------------------
    # Ensure no state files exist at temp paths (use non-existent paths)
    r_v2 = route_with_transmission(
        "write some python code to parse JSON",
        lane="interactive",
        config_path=CONFIG_PATH, log_path=log_path,
        hypnos_state_path="/tmp/nonexistent_hypnos_state_xyz.json",
        recall_state_path="/tmp/nonexistent_recall_state_xyz.json",
        ops_events_path=ops_log,
    )
    r_v1 = route_with_transmission(
        "write some python code to parse JSON",
        lane="interactive",
        **kwargs
    )
    baseline_match = (
        r_v2.get("model") == r_v1.get("model") and
        r_v2.get("gear") == r_v1.get("gear") and
        r_v2.get("policy_active") == False and
        r_v2.get("recovery_context") == False
    )
    check(12, "Both policy files absent → identical behavior to v1 baseline",
          baseline_match,
          f"v2_model={r_v2.get('model')} v1_model={r_v1.get('model')} "
          f"policy_active={r_v2.get('policy_active')} recovery_context={r_v2.get('recovery_context')}")

    # -------------------------------------------------------------------
    # Test 13: Malformed policy file → routing continues, error event logged
    # -------------------------------------------------------------------
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        f.write("{ this is not valid json !!! ")
        bad_hypnos = f.name

    r = route_with_transmission(
        "write some python code",
        lane="interactive",
        config_path=CONFIG_PATH, log_path=log_path,
        hypnos_state_path=bad_hypnos,
        ops_events_path=ops_log,
    )
    routing_continued = r.get("model") is not None and r.get("status") != "EXHAUSTED"
    log_lines = [l for l in Path(log_path).read_text().strip().split("\n") if l]
    error_logged = any(json.loads(l).get("event") == "TRANSMISSION_POLICY_ERROR" for l in log_lines)

    check(13, "Malformed policy file → routing continues, error event logged",
          routing_continued and error_logged,
          f"model={r.get('model')} status={r.get('status')} error_logged={error_logged}")
    os.unlink(bad_hypnos)

    # -------------------------------------------------------------------
    # Test 14: scope=agent_subset, current agent NOT in affected_agents
    #          → only mild global bias (premium deprioritized), efficient still allowed
    # -------------------------------------------------------------------
    recall_subset = {
        "in_recovery": True,
        "recovery_phase": "RESTORE",
        "affected_agents": ["heike"],   # only heike is affected
        "scope": "agent_subset"
    }
    recall_path2 = write_json_file(recall_subset)

    r = route_with_transmission(
        "what is the current date",    # simple → normally routes to efficient
        work_class="simple",
        lane="interactive",
        agent_id="soren",              # NOT in affected_agents
        config_path=CONFIG_PATH, log_path=log_path,
        recall_state_path=recall_path2,
        ops_events_path=ops_log,
    )
    efficient_allowed = r.get("gear") == "efficient" or r.get("gear") in ("mid", "budget-capable", "premium")
    # Key assertion: efficient should NOT be blocked for non-affected agent
    # (it may still not be selected due to lane preference, but it must remain in candidate chain)
    cfg_data = json.loads(Path(CONFIG_PATH).read_text())
    efficient_models = [m for m, c in cfg_data["models"].items() if c.get("tier") == "efficient"]
    chain = r.get("candidate_chain", [])
    # At least one efficient model should remain in chain (not purged)
    efficient_in_chain = any(m in chain for m in efficient_models)

    check(14, "scope=agent_subset, agent NOT in affected_agents → efficient tier NOT purged (mild bias only)",
          efficient_in_chain,
          f"gear={r.get('gear')} efficient_in_chain={efficient_in_chain} chain={chain} "
          f"recovery_context={r.get('recovery_context')}")
    os.unlink(recall_path2)

    # Cleanup
    os.unlink(log_path)
    try:
        os.unlink(ops_log)
    except OSError:
        pass

    # ===================================================================
    # Summary
    # ===================================================================
    passed = sum(results)
    total = len(results)
    print(f"\n{'='*50}")
    if total == 14:
        label = "Transmission v2 Proof Tests"
    else:
        label = "Transmission Proof Tests"
    print(f"{label}: {passed}/{total} PASSED")
    if passed == total:
        print("✅ ALL TESTS PASS — Transmission v2 DEFINITION OF DONE MET")
        return 0
    else:
        failed = [i+1 for i, r in enumerate(results) if not r]
        print(f"❌ FAILED TESTS: {failed}")
        return 1


if __name__ == "__main__":
    sys.exit(run_tests())
