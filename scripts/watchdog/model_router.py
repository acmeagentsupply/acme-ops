#!/usr/bin/env python3
"""
model_router.py — Client-side hard timeout + sequential failover chain

SphinxGate patch (lane enforcement + token logging):
  - route(..., lane=None, allow_premium=False)
  - lane resolution: explicit arg → OPENCLAW_LANE env → "interactive"
  - lane=background: strips Claude unless allow_premium=True or OPENCLAW_ALLOW_PREMIUM=1
  - background chain from agents.lanes.background.model in openclaw.json
  - tokens.log: one line per attempt (FAIL and OK), never crashes

Emits these log lines (to stdout + stall.log):
  [TS] MODEL_CHAIN_RESOLVED  source= chain= lane= req=
  [TS] MODEL_TIMEOUT_ABORT   provider= duration_ms= model= req=
  [TS] MODEL_FAILOVER_TRIGGERED from=p/m to=p/m req=
  [TS] MODEL_FAILOVER_SUCCESS   provider= duration_ms= model= req=
  [TS] MODEL_FAILOVER_EXHAUSTED req=
  [TS] MODEL_PROVIDER_ERROR     provider= error= req=
"""

import argparse
import http.client
import json
import os
import sys
import time
import threading
import uuid
from datetime import datetime, timedelta


# ── Paths ─────────────────────────────────────────────────────────────────────
WATCHDOG_DIR     = os.path.expanduser("~/.openclaw/watchdog")
STALL_LOG        = os.path.join(WATCHDOG_DIR, "stall.log")
MODEL_STATE_FILE = os.path.join(WATCHDOG_DIR, "model_state.json")
TOKENS_LOG       = os.path.expanduser("~/.openclaw/metrics/tokens.log")
AUTH_PROFILES    = os.path.expanduser(
    "~/.openclaw/agents/main/agent/auth-profiles.json"
)
OPENCLAW_JSON    = os.path.expanduser("~/.openclaw/openclaw.json")

DEFAULT_TIMEOUT_S = 90

# Default interactive failover chain — mirrors openclaw.json
DEFAULT_INTERACTIVE_CHAIN = [
    {"provider": "anthropic", "model": "claude-sonnet-4-6"},
    {"provider": "openai",    "model": "gpt-4.1-mini"},
    {"provider": "google",    "model": "gemini-2.5-flash-lite"},
    {"provider": "openrouter","model": "deepseek/deepseek-v3.1-terminus:exacto"},
]

# Default background chain — cheap-first, no Claude
DEFAULT_BACKGROUND_CHAIN = [
    {"provider": "openai",    "model": "gpt-4.1-mini"},
    {"provider": "google",    "model": "gemini-2.5-flash-lite"},
    {"provider": "openrouter","model": "deepseek/deepseek-v3.1-terminus:exacto"},
]

# Cost per 1k tokens (rough estimates USD)
COST_PER_1K = {
    "anthropic": 0.0045,
    "openai":    0.0002,
    "google":    0.0001,
    "openrouter":0.0001,
}


# ── Utilities ─────────────────────────────────────────────────────────────────

def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def emit(line, log_file=None):
    """Print to stdout and append to stall log."""
    print(line, flush=True)
    target = log_file or STALL_LOG
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _log_tokens(req_id, lane, provider, model, usage, status, dur_ms):
    """
    Append one line per attempt to tokens.log.
    usage dict: {"input": int, "output": int, "total": int} — use -1 if unavailable.
    status: "OK" or "FAIL"
    Never raises.
    """
    try:
        in_t  = usage.get("input",  -1)
        out_t = usage.get("output", -1)
        tot_t = usage.get("total",  -1)
        if tot_t > 0:
            cost = (tot_t / 1000.0) * COST_PER_1K.get(provider, 0.0001)
        else:
            cost = 0.0
        line = (
            f"{ts()},{req_id},{lane},{provider},{model},"
            f"{in_t},{out_t},{tot_t},{status},{dur_ms},${cost:.6f}"
        )
        os.makedirs(os.path.dirname(TOKENS_LOG), exist_ok=True)
        with open(TOKENS_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def save_model_state(provider, status):
    """
    Persist last model stats for watchdog heartbeat enrichment.
    No-op when TEST_MODE=1 env var is set (prevents test contamination).
    """
    if os.environ.get("TEST_MODE") == "1":
        return
    try:
        state = {
            "provider":   provider,
            "status":     status,
            "updated_at": time.time(),
        }
        with open(MODEL_STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def load_api_keys():
    keys = {}
    try:
        with open(AUTH_PROFILES) as f:
            profiles = json.load(f)
        for v in (profiles.values() if isinstance(profiles, dict) else profiles):
            if not isinstance(v, dict):
                continue
            prov = v.get("provider", "")
            key  = v.get("apiKey") or v.get("token") or v.get("accessToken")
            if prov and key and prov not in keys:
                keys[prov] = key
    except Exception:
        pass
    if "openai" not in keys:
        keys["openai"] = os.environ.get("OPENAI_API_KEY", "")
    return keys


# ── HTTP helper with hard timeout ─────────────────────────────────────────────

class TimeoutAbort(Exception):
    pass

class ProviderError(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


def _https_post(host, path, headers, body_bytes, timeout_s):
    conn      = http.client.HTTPSConnection(host, timeout=timeout_s + 10)
    timed_out = threading.Event()

    def _on_timeout():
        timed_out.set()
        try:
            conn.close()
        except Exception:
            pass

    timer = threading.Timer(timeout_s, _on_timeout)
    timer.start()
    try:
        conn.request("POST", path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        if timed_out.is_set():
            raise TimeoutAbort(f"Timed out after {timeout_s}s")
        return resp.status, data
    except (OSError, ConnectionError, http.client.HTTPException) as exc:
        if timed_out.is_set():
            raise TimeoutAbort(f"Timed out after {timeout_s}s") from exc
        raise ProviderError(str(exc))
    finally:
        timer.cancel()
        try:
            conn.close()
        except Exception:
            pass


# ── Provider callers — return (text, usage_dict) ──────────────────────────────
# usage_dict: {"input": int, "output": int, "total": int}

def _call_anthropic(prompt, model, api_key, timeout_s):
    body = json.dumps({
        "model": model,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    headers = {
        "Content-Type":      "application/json",
        "x-api-key":         api_key,
        "anthropic-version": "2023-06-01",
        "Content-Length":    str(len(body)),
    }
    status, data = _https_post("api.anthropic.com", "/v1/messages",
                               headers, body, timeout_s)
    if status not in (200, 201):
        raise ProviderError(f"HTTP {status}: {data[:120]}", status_code=status)
    resp = json.loads(data)
    text  = resp["content"][0]["text"]
    u     = resp.get("usage", {})
    usage = {
        "input":  u.get("input_tokens",  -1),
        "output": u.get("output_tokens", -1),
        "total":  u.get("input_tokens", 0) + u.get("output_tokens", 0) or -1,
    }
    return text, usage


def _call_openai(prompt, model, api_key, timeout_s):
    body = json.dumps({
        "model":             model,
        "input":             prompt,
        "max_output_tokens": 256,
    }).encode()
    headers = {
        "Content-Type":   "application/json",
        "Authorization":  f"Bearer {api_key}",
        "Content-Length": str(len(body)),
    }
    status, data = _https_post("api.openai.com", "/v1/responses",
                               headers, body, timeout_s)
    if status not in (200, 201):
        raise ProviderError(f"HTTP {status}: {data[:120]}", status_code=status)
    resp = json.loads(data)
    text = resp.get("output", [{}])[0].get("content", [{}])[0].get("text", "")
    u    = resp.get("usage", {})
    usage = {
        "input":  u.get("input_tokens",  -1),
        "output": u.get("output_tokens", -1),
        "total":  u.get("total_tokens",  -1),
    }
    return text, usage


def _call_google(prompt, model, api_key, timeout_s):
    path = f"/v1beta/models/{model}:generateContent?key={api_key}"
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}]
    }).encode()
    headers = {
        "Content-Type":   "application/json",
        "Content-Length": str(len(body)),
    }
    status, data = _https_post(
        "generativelanguage.googleapis.com", path, headers, body, timeout_s
    )
    if status not in (200, 201):
        raise ProviderError(f"HTTP {status}: {data[:120]}", status_code=status)
    resp  = json.loads(data)
    text  = resp["candidates"][0]["content"]["parts"][0]["text"]
    u     = resp.get("usageMetadata", {})
    usage = {
        "input":  u.get("promptTokenCount",     -1),
        "output": u.get("candidatesTokenCount", -1),
        "total":  u.get("totalTokenCount",      -1),
    }
    return text, usage


_REAL_CALLERS = {
    "anthropic": _call_anthropic,
    "openai":    _call_openai,
    "google":    _call_google,
}


# ── Chain config loader ───────────────────────────────────────────────────────

def _parse_provider_model(ref):
    """
    Parse 'provider/model' string → {"provider": ..., "model": ...}.
    Handles openrouter/deepseek/x:tag style (first segment is provider).
    """
    if isinstance(ref, dict):
        return ref  # already parsed
    parts = ref.split("/", 1)
    if len(parts) == 2:
        return {"provider": parts[0], "model": parts[1]}
    return {"provider": "unknown", "model": ref}


_POLICY_CACHE = {}          # {cfg_path: (mtime, {lane: policy})}
_POLICY_CACHE_TTL_S = 60   # re-read config at most once per minute

def load_sphinxgate_policy(lane, openclaw_json=None):
    """
    Load SphinxGate lane policy from openclaw.json sphinxgate.lanes.<lane>.
    Cached by file mtime — re-read only when config changes or TTL expires.
    Returns empty dict (no policy) if sphinxgate block missing — backward compat.
    Never raises.
    """
    try:
        cfg_path = openclaw_json or OPENCLAW_JSON
        now = time.time()
        cached = _POLICY_CACHE.get(cfg_path)
        try:
            mtime = os.path.getmtime(cfg_path)
        except Exception:
            mtime = 0

        if cached and cached[0] == mtime and (now - cached[2]) < _POLICY_CACHE_TTL_S:
            return cached[1].get(lane, {})

        # Cache miss — read config
        with open(cfg_path) as f:
            cfg = json.load(f)
        sg = cfg.get("sphinxgate", {})
        lanes_cfg = sg.get("lanes", {}) if sg.get("enabled", False) else {}
        fail_open = sg.get("failover", {}).get("fail_open", False)
        all_policies = {}
        for ln, lc in lanes_cfg.items():
            all_policies[ln] = {
                "allow_providers": lc.get("allow_providers") or None,
                "deny_providers":  lc.get("deny_providers")  or None,
                "fail_open":       fail_open,
            }
        _POLICY_CACHE[cfg_path] = (mtime, all_policies, now)
        return all_policies.get(lane, {})
    except Exception:
        return {}


def _apply_sphinxgate_policy(chain, lane, policy, log_file, req_id):
    """
    Filter chain by SphinxGate policy. Returns (filtered_chain, exhausted).

    Precedence rules (v1):
      1. allow_providers (if present) is AUTHORITATIVE — only listed providers pass
      2. deny_providers SUBTRACTS from the result of step 1
      3. Order: allow filter first, then deny filter

    Exhaustion:
      - If filtering empties the chain → emits SPHINXGATE_POLICY_EXHAUSTED
      - Returns ([], True) so caller can handle fail_open behavior

    Emits:
      SPHINXGATE_LANE_RESOLVED  — once per request
      SPHINXGATE_PROVIDER_STRIPPED — once per stripped entry (with reason)
      SPHINXGATE_POLICY_EXHAUSTED  — if chain becomes empty
    """
    allow     = policy.get("allow_providers")
    deny      = policy.get("deny_providers")
    fail_open = policy.get("fail_open", False)

    if not allow and not deny:
        emit(
            f"[{ts()}] SPHINXGATE_LANE_RESOLVED"
            f" lane={lane} policy=none req={req_id}",
            log_file,
        )
        return chain, False

    emit(
        f"[{ts()}] SPHINXGATE_LANE_RESOLVED"
        f" lane={lane} allow={allow} deny={deny} fail_open={fail_open} req={req_id}",
        log_file,
    )

    filtered = []
    for entry in chain:
        provider = entry.get("provider", "")
        # Step 1: allow list is authoritative
        if allow and provider not in allow:
            emit(
                f"[{ts()}] SPHINXGATE_PROVIDER_STRIPPED"
                f" provider={provider} model={entry.get('model','')} reason=not_in_allow_list"
                f" lane={lane} req={req_id}",
                log_file,
            )
            continue
        # Step 2: deny list subtracts
        if deny and provider in deny:
            emit(
                f"[{ts()}] SPHINXGATE_PROVIDER_STRIPPED"
                f" provider={provider} model={entry.get('model','')} reason=deny_list"
                f" lane={lane} req={req_id}",
                log_file,
            )
            continue
        filtered.append(entry)

    if not filtered:
        emit(
            f"[{ts()}] SPHINXGATE_POLICY_EXHAUSTED"
            f" lane={lane} allow={allow} deny={deny} fail_open={fail_open} req={req_id}",
            log_file,
        )
        return [], True

    return filtered, False


def load_chain_from_config(openclaw_json=None):
    """
    Read interactive failover chain from openclaw.json.
    Returns (chain, source).
    """
    cfg_path = openclaw_json or OPENCLAW_JSON
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        model_cfg = cfg.get("agents", {}).get("defaults", {}).get("model", {})
        primary   = model_cfg.get("primary", "")
        fallbacks = model_cfg.get("fallbacks", [])
        if not primary:
            return DEFAULT_INTERACTIVE_CHAIN[:], "default"
        chain = [_parse_provider_model(primary)]
        for fb in fallbacks:
            chain.append(_parse_provider_model(fb))
        return chain, "config"
    except Exception:
        return DEFAULT_INTERACTIVE_CHAIN[:], "default"


def load_background_chain(openclaw_json=None):
    """
    Read background lane chain from agents.lanes.background.model in openclaw.json.
    Falls back to DEFAULT_BACKGROUND_CHAIN if missing.
    """
    cfg_path = openclaw_json or OPENCLAW_JSON
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        bg_models = (
            cfg.get("agents", {})
               .get("lanes", {})
               .get("background", {})
               .get("model", [])
        )
        if not bg_models:
            return DEFAULT_BACKGROUND_CHAIN[:], "default_bg"
        return [_parse_provider_model(m) for m in bg_models], "config_bg"
    except Exception:
        return DEFAULT_BACKGROUND_CHAIN[:], "default_bg"


# ── Core router ───────────────────────────────────────────────────────────────

def route(
    prompt,
    chain=None,
    timeout_s=DEFAULT_TIMEOUT_S,
    req_id=None,
    log_file=None,
    mock_fns=None,
    lane=None,
    allow_premium=False,
):
    """
    Route prompt through provider chain with hard timeout + sequential failover.

    SphinxGate additions:
      lane: "interactive" | "background" | "critical"
            resolved from: explicit arg → OPENCLAW_LANE env → "interactive"
      allow_premium: bypass Claude block in background lane
      mock_fns: {provider: callable(prompt, cancel_event) -> (text, usage)}

    Returns dict: {text, provider, model, dur_ms, status, req_id}
    tokens.log: one line per attempt (status=FAIL or status=OK)
    """
    # ── Lane resolution ──────────────────────────────────────────────────────
    if lane is None:
        lane = os.environ.get("OPENCLAW_LANE", "interactive")

    # ── Chain selection ──────────────────────────────────────────────────────
    chain_source = "caller"
    if chain is None:
        if lane == "background":
            chain, chain_source = load_background_chain()
        else:
            chain, chain_source = load_chain_from_config()

    if req_id is None:
        req_id = uuid.uuid4().hex[:8]

    # ── SphinxGate policy enforcement (config-driven, no hardcoding) ─────────
    policy = load_sphinxgate_policy(lane)
    # allow_premium / OPENCLAW_ALLOW_PREMIUM bypasses deny list only
    if allow_premium or os.environ.get("OPENCLAW_ALLOW_PREMIUM"):
        policy = {k: v for k, v in policy.items() if k != "deny_providers"}
    original_chain = chain[:]  # preserve for fail_open fallback
    chain, exhausted = _apply_sphinxgate_policy(chain, lane, policy, log_file, req_id)

    if exhausted:
        fail_open = policy.get("fail_open", False)
        if fail_open:
            emit(
                f"[{ts()}] SPHINXGATE_FAILOPEN_FALLBACK"
                f" lane={lane} routing=original_chain req={req_id}",
                log_file,
            )
            chain = original_chain
        else:
            emit(
                f"[{ts()}] SPHINXGATE_POLICY_HARD_FAIL lane={lane} req={req_id}",
                log_file,
            )
            return {"text": None, "status": "POLICY_FAIL", "req_id": req_id}

    chain_str = " -> ".join(f"{e['provider']}/{e['model']}" for e in chain)
    emit(
        f"[{ts()}] MODEL_CHAIN_RESOLVED source={chain_source}"
        f" chain=[{chain_str}] lane={lane} req={req_id}",
        log_file,
    )

    api_keys       = {} if mock_fns else load_api_keys()
    seen_providers = set()

    for i, entry in enumerate(chain):
        provider = entry["provider"]
        model    = entry["model"]

        if provider in seen_providers:
            emit(
                f"[{ts()}] MODEL_ROUTER_SKIP provider={provider}"
                f" reason=duplicate req={req_id}",
                log_file,
            )
            continue
        seen_providers.add(provider)

        start_s      = time.time()
        cancel_event = threading.Event()
        result_box   = [None]   # (text, usage)
        error_box    = [None]

        # ── Build worker ─────────────────────────────────────────────────────
        if mock_fns and provider in mock_fns:
            mock = mock_fns[provider]
            def _worker(_m=mock, _ce=cancel_event):
                try:
                    result_box[0] = _m(prompt, _ce)
                except Exception as exc:
                    error_box[0] = exc
        else:
            def _worker(_p=provider, _mo=model, _ts=timeout_s):
                try:
                    key = api_keys.get(_p, "")
                    if not key:
                        raise ProviderError(f"No API key for {_p}")
                    caller = _REAL_CALLERS.get(_p)
                    if caller is None:
                        raise ProviderError(f"Unsupported provider: {_p}")
                    result_box[0] = caller(prompt, _mo, key, _ts)
                except (TimeoutAbort, ProviderError) as exc:
                    error_box[0] = exc
                except Exception as exc:
                    error_box[0] = ProviderError(str(exc))

        def _abort_timer(_ce=cancel_event):
            _ce.set()

        timer  = threading.Timer(timeout_s, _abort_timer)
        thread = threading.Thread(target=_worker, daemon=True)
        timer.start()
        thread.start()
        thread.join(timeout=timeout_s + 2)
        timer.cancel()

        dur_ms    = int((time.time() - start_s) * 1000)
        timed_out = cancel_event.is_set() or thread.is_alive()
        has_error = error_box[0] is not None

        # ── Timeout path ─────────────────────────────────────────────────────
        if timed_out or isinstance(error_box[0], TimeoutAbort):
            emit(
                f"[{ts()}] MODEL_TIMEOUT_ABORT"
                f" provider={provider} duration_ms={dur_ms}"
                f" model={model} req={req_id}",
                log_file,
            )
            _log_tokens(req_id, lane, provider, model,
                        {"input": -1, "output": -1, "total": -1}, "FAIL", dur_ms)
            if i + 1 < len(chain):
                nxt = chain[i + 1]
                emit(
                    f"[{ts()}] MODEL_FAILOVER_TRIGGERED"
                    f" from={provider}/{model}"
                    f" to={nxt['provider']}/{nxt['model']}"
                    f" req={req_id}",
                    log_file,
                )
            save_model_state(provider, "timeout")
            continue

        # ── Error path ───────────────────────────────────────────────────────
        if has_error:
            err_str = str(error_box[0])[:100]
            emit(
                f"[{ts()}] MODEL_PROVIDER_ERROR"
                f" provider={provider} error={err_str} req={req_id}",
                log_file,
            )
            _log_tokens(req_id, lane, provider, model,
                        {"input": -1, "output": -1, "total": -1}, "FAIL", dur_ms)
            if i + 1 < len(chain):
                nxt = chain[i + 1]
                emit(
                    f"[{ts()}] MODEL_FAILOVER_TRIGGERED"
                    f" from={provider}/{model}"
                    f" to={nxt['provider']}/{nxt['model']}"
                    f" req={req_id}",
                    log_file,
                )
            save_model_state(provider, "error")
            continue

        # ── Success ──────────────────────────────────────────────────────────
        text, usage = result_box[0]
        ok_status   = "OK" if i == 0 else "OK_FAILOVER"

        if i > 0:
            emit(
                f"[{ts()}] MODEL_FAILOVER_SUCCESS"
                f" provider={provider} duration_ms={dur_ms}"
                f" model={model} req={req_id}",
                log_file,
            )

        _log_tokens(req_id, lane, provider, model, usage, ok_status, dur_ms)
        save_model_state(provider, ok_status.lower())

        return {
            "text":     text,
            "provider": provider,
            "model":    model,
            "dur_ms":   dur_ms,
            "status":   ok_status,
            "req_id":   req_id,
            "usage":    usage,
        }

    # ── Exhausted ─────────────────────────────────────────────────────────────
    emit(f"[{ts()}] MODEL_FAILOVER_EXHAUSTED req={req_id}", log_file)
    save_model_state("none", "failover_exhausted")
    return {"text": None, "status": "EXHAUSTED", "req_id": req_id}


# ── tokens-status ─────────────────────────────────────────────────────────────

def cmd_tokens_status():
    cutoff = datetime.now() - timedelta(hours=1)
    rows   = []
    try:
        with open(TOKENS_LOG) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                parts = raw.split(",")
                if len(parts) < 11:
                    continue
                try:
                    row_ts = datetime.strptime(parts[0], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    continue
                if row_ts >= cutoff:
                    rows.append({
                        "ts":       parts[0],
                        "req_id":   parts[1],
                        "lane":     parts[2],
                        "provider": parts[3],
                        "model":    parts[4],
                        "in":       int(parts[5]),
                        "out":      int(parts[6]),
                        "total":    int(parts[7]),
                        "status":   parts[8],
                        "dur_ms":   parts[9],
                        "cost":     parts[10],
                    })
    except FileNotFoundError:
        print("No tokens log found at", TOKENS_LOG)
        return

    if not rows:
        print("No token log entries in last 60 minutes.")
        return

    in_sum      = sum(r["in"]    for r in rows if r["in"]    > 0)
    out_sum     = sum(r["out"]   for r in rows if r["out"]   > 0)
    total_sum   = sum(r["total"] for r in rows if r["total"] > 0)
    claude_sum  = sum(r["total"] for r in rows if r["total"] > 0 and "claude" in r["model"])
    ok_rows     = [r for r in rows if r["status"].startswith("OK")]
    fail_rows   = [r for r in rows if r["status"] == "FAIL"]
    claude_pct  = (claude_sum / total_sum * 100) if total_sum > 0 else 0.0
    state       = "THROTTLED" if total_sum >= 20000 else "NORMAL"

    print(f"=== SphinxGate tokens-status (last 60 min) ===")
    print(f"Window : {cutoff.strftime('%H:%M:%S')} → now")
    print(f"Calls  : {len(rows)} total  ({len(ok_rows)} OK, {len(fail_rows)} FAIL)")
    print(f"Tokens : in={in_sum}  out={out_sum}  total={total_sum}")
    print(f"Claude : {claude_sum} tokens  ({claude_pct:.1f}% of total)")
    print(f"State  : {state}")
    print()
    print("Last 5 calls:")
    print(f"  {'ts':<20} {'lane':<12} {'provider':<12} {'model':<30} {'status':<12} {'total':>6}  cost")
    print(f"  {'-'*20} {'-'*12} {'-'*12} {'-'*30} {'-'*12} {'-'*6}  ----")
    for r in rows[-5:]:
        print(
            f"  {r['ts']:<20} {r['lane']:<12} {r['provider']:<12}"
            f" {r['model']:<30} {r['status']:<12} {r['total']:>6}  {r['cost']}"
        )


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="model_router — SphinxGate edition")
    p.add_argument("prompt", nargs="?", default="Reply with one word: OK")
    p.add_argument("--timeout",       type=int, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--req-id",        default=None)
    p.add_argument("--lane",          default=None, help="interactive|background|critical")
    p.add_argument("--allow-premium", action="store_true")
    p.add_argument("--tokens-status", action="store_true", help="Show last 60-min token summary")
    args = p.parse_args()

    if args.tokens_status:
        cmd_tokens_status()
    else:
        result = route(
            args.prompt,
            timeout_s=args.timeout,
            req_id=args.req_id,
            lane=args.lane,
            allow_premium=args.allow_premium,
        )
        print(f"\nFinal: provider={result.get('provider')} model={result.get('model')}"
              f" status={result.get('status')} dur_ms={result.get('dur_ms')}"
              f" usage={result.get('usage')}")
