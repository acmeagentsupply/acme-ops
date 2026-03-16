"""
Transmission v2 — Cognitive Execution Router with Policy Resolver
Arch/Ops Approved Build Packet 2026-03-15

v1: Deterministic routing by work_class, lane preference, capability masking.
v2: Activates Policy Resolver — optional read-only Hypnos and Recall state signals.

Policy signals are optional. If absent, unreadable, or malformed, Transmission
behaves exactly like v1. No blocking runtime dependencies.

Zero writes to openclaw.json. Read-only. Deterministic. Observable.
Target: p95 routing latency < 10ms (heuristic path).
"""

import json
import os
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORK_CLASS_PATTERNS: dict[str, list[str]] = {
    "coding":     ["code", "function", "debug", "refactor", "implement", "python", "class", "error", "test", "fix"],
    "analysis":   ["analyze", "evaluate", "compare", "investigate", "assess", "research", "explain why", "reason"],
    "writing":    ["write", "draft", "summarize", "document", "email", "report", "describe", "essay"],
    "organizing": ["list", "sort", "classify", "format", "json", "structure", "table", "parse", "extract"],
    "simple":     ["what is", "when", "who", "yes or no", "confirm", "check if", "how many"],
    "creative":   ["brainstorm", "imagine", "generate ideas", "suggest", "creative", "invent"],
}

EXECUTION_DEFAULTS: dict[str, dict] = {
    "coding":     {"mode": "stream", "temperature": 0.1, "tool_calling": True,  "structured_output": False},
    "analysis":   {"mode": "stream", "temperature": 0.3, "tool_calling": True,  "structured_output": False},
    "writing":    {"mode": "stream", "temperature": 0.5, "tool_calling": False, "structured_output": False},
    "organizing": {"mode": "batch",  "temperature": 0.0, "tool_calling": False, "structured_output": True},
    "simple":     {"mode": "batch",  "temperature": 0.2, "tool_calling": False, "structured_output": False},
    "creative":   {"mode": "stream", "temperature": 0.8, "tool_calling": False, "structured_output": False},
}

TIER_ORDER = ["premium", "budget-capable", "mid", "efficient"]

DEFAULT_CONFIG_PATH       = Path.home() / ".openclaw" / "watchdog" / "transmission_config.json"
DEFAULT_LOG_PATH          = Path.home() / ".openclaw" / "watchdog" / "transmission_events.log"
DEFAULT_HYPNOS_STATE_PATH = Path.home() / ".openclaw" / "watchdog" / "hypnos_state.json"
DEFAULT_RECALL_STATE_PATH = Path.home() / ".openclaw" / "watchdog" / "recall_state.json"
DEFAULT_OPS_EVENTS_PATH   = Path.home() / ".openclaw" / "watchdog" / "ops_events.ndjson"

# ---------------------------------------------------------------------------
# LRU Classification Cache
# ---------------------------------------------------------------------------

class LRUCache:
    def __init__(self, capacity: int):
        self.cache: OrderedDict = OrderedDict()
        self.capacity = max(1, capacity)

    def get(self, key: str):
        if key not in self.cache:
            return None
        self.cache.move_to_end(key)
        return self.cache[key]

    def put(self, key: str, value):
        if key in self.cache:
            self.cache.move_to_end(key)
        self.cache[key] = value
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)


# ---------------------------------------------------------------------------
# Config Loader (cached, mtime-aware)
# ---------------------------------------------------------------------------

_config_cache: dict = {}
_config_mtime: float = 0.0
_classification_cache: Optional[LRUCache] = None


def _load_config(config_path: Optional[Path] = None) -> dict:
    global _config_cache, _config_mtime, _classification_cache
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return _config_cache or {}
    if mtime != _config_mtime or not _config_cache:
        with open(path) as f:
            _config_cache = json.load(f)
        _config_mtime = mtime
        cache_size = _config_cache.get("classification_cache_size", 100)
        _classification_cache = LRUCache(cache_size)
    return _config_cache


# ---------------------------------------------------------------------------
# v2: Policy State Loaders (mtime-cached, same discipline as config)
# ---------------------------------------------------------------------------

_hypnos_state_cache: dict = {}
_hypnos_state_mtime: float = 0.0
_recall_state_cache: dict = {}
_recall_state_mtime: float = 0.0


def _read_hypnos_state(
    path: Optional[Path] = None,
    log_path: Optional[Path] = None,
    req_id: str = "",
) -> dict:
    """
    Load Hypnos routing projection. Cached; reloads only on file mtime change.
    Returns {} if file absent, unreadable, or malformed — never raises.
    """
    global _hypnos_state_cache, _hypnos_state_mtime
    p = Path(path) if path else DEFAULT_HYPNOS_STATE_PATH
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    if mtime == _hypnos_state_mtime and _hypnos_state_cache:
        return _hypnos_state_cache
    try:
        with open(p) as f:
            data = json.load(f)
        _hypnos_state_cache = data
        _hypnos_state_mtime = mtime
        return data
    except Exception as e:
        # Malformed — emit error, discard, fallback to v1
        if log_path and req_id:
            _emit("TRANSMISSION_POLICY_ERROR", req_id, log_path,
                  source="hypnos", error=str(e))
        _hypnos_state_cache = {}
        _hypnos_state_mtime = mtime  # remember mtime so we don't retry on every call
        return {}


def _read_recall_state(
    path: Optional[Path] = None,
    log_path: Optional[Path] = None,
    req_id: str = "",
) -> dict:
    """
    Load Recall recovery projection. Cached; reloads only on file mtime change.
    Returns {} if file absent, unreadable, or malformed — never raises.
    """
    global _recall_state_cache, _recall_state_mtime
    p = Path(path) if path else DEFAULT_RECALL_STATE_PATH
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    if mtime == _recall_state_mtime and _recall_state_cache:
        return _recall_state_cache
    try:
        with open(p) as f:
            data = json.load(f)
        _recall_state_cache = data
        _recall_state_mtime = mtime
        return data
    except Exception as e:
        if log_path and req_id:
            _emit("TRANSMISSION_POLICY_ERROR", req_id, log_path,
                  source="recall", error=str(e))
        _recall_state_cache = {}
        _recall_state_mtime = mtime
        return {}


# ---------------------------------------------------------------------------
# v2: Policy Resolver — Recall Restrictions
# ---------------------------------------------------------------------------

def _apply_recall_restrictions(
    chain: list[str],
    recall_state: dict,
    agent_id: Optional[str],
    models: dict,
) -> tuple[list[str], bool]:
    """
    Apply Recall recovery routing constraints to the candidate chain.

    Restriction levels:
    - scope=global OR (scope=agent_subset AND agent in affected_agents):
        STRONG — remove efficient tier, deprioritize premium (move to end)
    - scope=agent_subset AND agent NOT in affected_agents:
        MILD — deprioritize premium only; efficient stays

    Missing scope defaults to "global" (safer posture).
    Returns (modified_chain, policy_applied).
    """
    if not recall_state.get("in_recovery"):
        return chain, False

    scope = recall_state.get("scope", "global")  # absent → global (safer default)
    affected = recall_state.get("affected_agents", [])
    agent_is_affected = bool(agent_id and agent_id in affected)

    strong = (scope == "global") or (scope == "agent_subset" and agent_is_affected)
    mild   = (scope == "agent_subset" and not agent_is_affected)

    modified = list(chain)
    policy_applied = False

    if strong:
        # Remove efficient tier entirely
        before = len(modified)
        modified = [m for m in modified if models.get(m, {}).get("tier") != "efficient"]
        if len(modified) < before:
            policy_applied = True
        # Deprioritize premium: move to end of chain
        premium     = [m for m in modified if models.get(m, {}).get("tier") == "premium"]
        non_premium = [m for m in modified if models.get(m, {}).get("tier") != "premium"]
        if premium and non_premium:
            modified = non_premium + premium
            policy_applied = True

    elif mild:
        # Deprioritize premium only — efficient stays
        premium     = [m for m in modified if models.get(m, {}).get("tier") == "premium"]
        non_premium = [m for m in modified if models.get(m, {}).get("tier") != "premium"]
        if premium and non_premium:
            modified = non_premium + premium
            policy_applied = True

    return modified, policy_applied


# ---------------------------------------------------------------------------
# v2: Policy Resolver — Hypnos Restrictions
# ---------------------------------------------------------------------------

def _apply_hypnos_restrictions(
    chain: list[str],
    hypnos_state: dict,
    models: dict,
    required_features: Optional[dict],
) -> tuple[list[str], bool]:
    """
    Apply Hypnos governance constraints to the candidate chain.

    Applied in order:
    1. denied_providers  — hard exclude
    2. force_tier        — keep only specified tier
    3. cost_hold         — reorder: budget-capable/efficient first
    4. required_features — merge with caller's feature requirements
    5. preferred_tiers   — reorder: listed tiers first

    Returns (modified_chain, policy_applied).
    """
    if not hypnos_state.get("active"):
        return chain, False

    routing = hypnos_state.get("routing", {})
    modified = list(chain)
    policy_applied = False

    # 1. denied_providers: hard exclude
    denied = routing.get("denied_providers", [])
    if denied:
        before = len(modified)
        modified = [m for m in modified if models.get(m, {}).get("provider") not in denied]
        if len(modified) < before:
            policy_applied = True

    # 2. force_tier: keep only that tier
    force_tier = routing.get("force_tier")
    if force_tier:
        before = len(modified)
        modified = [m for m in modified if models.get(m, {}).get("tier") == force_tier]
        if len(modified) < before:
            policy_applied = True

    # 3. cost_hold: prefer budget-capable and efficient (reorder)
    if routing.get("cost_hold"):
        cheap = [m for m in modified if models.get(m, {}).get("tier") in ("budget-capable", "efficient")]
        mid   = [m for m in modified if models.get(m, {}).get("tier") == "mid"]
        prem  = [m for m in modified if models.get(m, {}).get("tier") == "premium"]
        if cheap and (mid or prem):
            modified = cheap + mid + prem
            policy_applied = True

    # 4. required_features (from Hypnos routing): merge-filter with caller features
    hypnos_feats = routing.get("required_features", {})
    all_feats = dict(required_features or {})
    all_feats.update({k: v for k, v in hypnos_feats.items() if v})
    if all_feats:
        before = len(modified)
        filtered = []
        for m in modified:
            mcfg = models.get(m, {})
            if all(mcfg.get(feat, False) for feat, val in all_feats.items() if val):
                filtered.append(m)
        modified = filtered
        if len(modified) < before:
            policy_applied = True

    # 5. preferred_tiers: bring listed tiers to front
    preferred = routing.get("preferred_tiers", [])
    if preferred:
        front = [m for m in modified if models.get(m, {}).get("tier") in preferred]
        back  = [m for m in modified if models.get(m, {}).get("tier") not in preferred]
        if front and back:
            modified = front + back
            policy_applied = True

    return modified, policy_applied


# ---------------------------------------------------------------------------
# v2: Ops Event Emitter (best-effort, never blocks routing)
# ---------------------------------------------------------------------------

def _emit_ops_event(
    req_id: str,
    model: str,
    work_class: str,
    gear: str,
    policy_active: bool,
    recovery_context: bool,
    duration_ms: float,
    ops_path: Optional[Path] = None,
) -> None:
    """
    Emit condensed routing event to shared stack event bus.
    Best-effort append only. Never raises. Never blocks routing.
    """
    try:
        path = Path(ops_path) if ops_path else DEFAULT_OPS_EVENTS_PATH
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "source": "transmission",
            "event": "ROUTE",
            "req_id": req_id,
            "model": model,
            "work_class": work_class,
            "gear": gear,
            "policy_active": policy_active,
            "recovery_context": recovery_context,
            "duration_ms": duration_ms,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # best-effort — ops bus failure must never fail routing


# ---------------------------------------------------------------------------
# Event Logger
# ---------------------------------------------------------------------------

def _emit(event: str, req_id: str, log_path: Path, **payload):
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "req_id": req_id,
        "event": event,
        **payload,
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Heuristic Classifier
# ---------------------------------------------------------------------------

def _classify_heuristic(prompt: str, work_classes: list[str]) -> tuple[str, float, str]:
    """
    Returns (work_class, confidence, source).
    confidence = hits_for_winner / len(pattern_list_for_winner)
    """
    prompt_lower = prompt.lower()
    hits: dict[str, int] = {}
    for wc in work_classes:
        patterns = WORK_CLASS_PATTERNS.get(wc, [])
        count = sum(1 for p in patterns if p in prompt_lower)
        if count > 0:
            hits[wc] = count

    if not hits:
        return "", 0.0, "heuristic"

    max_hits = max(hits.values())
    winners = [wc for wc, h in hits.items() if h == max_hits]
    winner = winners[0]

    max_possible = len(WORK_CLASS_PATTERNS.get(winner, []))
    confidence = max_hits / max_possible if max_possible > 0 else 0.0

    return winner, min(confidence, 1.0), "heuristic"


# ---------------------------------------------------------------------------
# Candidate Chain Builder
# ---------------------------------------------------------------------------

def _tier_rank(tier: str, lane_prefs: list[str]) -> int:
    try:
        return lane_prefs.index(tier)
    except ValueError:
        return len(lane_prefs)


def _build_candidate_chain(
    work_class: str,
    lane: str,
    models: dict,
    lane_preferences: dict,
    required_features: Optional[dict],
    gear_up: bool,
) -> list[str]:
    """
    Build ordered candidate list (v1 logic — unchanged in v2).
    Policy Resolver is applied separately after this step.
    """
    lane_prefs: list[str] = lane_preferences.get(lane, TIER_ORDER)

    candidates = []
    for model_id, cfg in models.items():
        if not cfg.get("enabled", True):
            continue
        if work_class not in cfg.get("capabilities", []):
            continue
        if required_features:
            skip = False
            for feat, val in required_features.items():
                if val and not cfg.get(feat, False):
                    skip = True
                    break
            if skip:
                continue
        candidates.append((model_id, cfg))

    if not candidates:
        return []

    # gear_up: drop lowest-tier candidates
    if gear_up and len(candidates) > 1:
        tiers_present = sorted(
            set(_tier_rank(c[1].get("tier", ""), lane_prefs) for c in candidates)
        )
        if len(tiers_present) > 1:
            worst_rank = tiers_present[-1]
            candidates = [c for c in candidates if _tier_rank(c[1].get("tier", ""), lane_prefs) < worst_rank]

    candidates.sort(key=lambda x: (
        _tier_rank(x[1].get("tier", ""), lane_prefs),
        -x[1].get("quality_score", 0),
        x[1].get("cost_weight", 999),
        x[1].get("latency_ms_p50", 999999),
    ))

    return [c[0] for c in candidates]


# ---------------------------------------------------------------------------
# Execution Config Builder
# ---------------------------------------------------------------------------

def _build_execution_config(work_class: str, model_cfg: dict) -> dict:
    defaults = dict(EXECUTION_DEFAULTS.get(work_class, EXECUTION_DEFAULTS["simple"]))
    if not model_cfg.get("tool_calling", False):
        defaults["tool_calling"] = False
    if not model_cfg.get("structured_output", False):
        defaults["structured_output"] = False
    defaults["context_window"] = model_cfg.get("context_window", 32000)
    return defaults


# ---------------------------------------------------------------------------
# Primary Interface
# ---------------------------------------------------------------------------

def route_with_transmission(
    prompt: str,
    work_class: Optional[str] = None,
    dispatch_hint: Optional[dict] = None,
    agent_metadata: Optional[dict] = None,
    lane: str = "interactive",
    req_id: Optional[str] = None,
    required_features: Optional[dict] = None,
    agent_id: Optional[str] = None,
    config_path: Optional[str] = None,
    log_path: Optional[str] = None,
    hypnos_state_path: Optional[str] = None,
    recall_state_path: Optional[str] = None,
    ops_events_path: Optional[str] = None,
) -> dict:
    """
    Route a task to the most appropriate model.

    v2 resolver sequence (6 steps):
      1. Build candidate chain using v1 logic
      2. Apply Recall recovery restrictions
      3. Apply Hypnos governance restrictions
      4. Re-rank remaining candidates (order preserved from steps 2-3)
      5. Emit TRANSMISSION_POLICY_REDUCED if chain changed
      6. Select first candidate

    Policy Resolver is activated only when state files are present.
    If both files are absent: identical behavior to v1.

    Returns dict with: model, provider, candidate_chain, work_class,
    confidence, classifier_source, execution_config, gear, duration_ms,
    req_id, policy_active, recovery_context.

    On total failure: status="EXHAUSTED" with TRANSMISSION_EXHAUSTED emitted.
    Never raises. Never writes to openclaw.json.
    """
    t0 = time.perf_counter()
    req_id = req_id or f"tr-{uuid.uuid4().hex[:8]}"
    _log = Path(log_path) if log_path else DEFAULT_LOG_PATH

    cfg = _load_config(Path(config_path) if config_path else None)
    models: dict = cfg.get("models", {})
    work_classes: list[str] = cfg.get("work_classes", list(WORK_CLASS_PATTERNS.keys()))
    confidence_threshold: float = cfg.get("confidence_threshold", 0.70)
    lane_preferences: dict = cfg.get("lane_preferences", {
        "interactive": ["premium", "budget-capable", "mid", "efficient"],
        "background":  ["budget-capable", "mid", "premium", "efficient"],
    })
    gear_up_on_low: bool = cfg.get("defaults", {}).get("gear_up_on_low_confidence", True)

    # --- Resolve work_class ---
    resolved_wc: str = ""
    confidence: float = 1.0
    classifier_source: str = "default"

    if dispatch_hint and isinstance(dispatch_hint, dict):
        dh_wc = dispatch_hint.get("work_class", "")
        if dh_wc and dh_wc in work_classes:
            resolved_wc = dh_wc
            classifier_source = "dispatch"
            confidence = 1.0

    if not resolved_wc and work_class and work_class in work_classes:
        resolved_wc = work_class
        classifier_source = "explicit"
        confidence = 1.0

    if not resolved_wc and agent_metadata and isinstance(agent_metadata, dict):
        am_wc = agent_metadata.get("work_class", "")
        if am_wc and am_wc in work_classes:
            resolved_wc = am_wc
            classifier_source = "agent_metadata"
            confidence = 1.0

    if not resolved_wc:
        cache_key = prompt[:200]
        cached = _classification_cache.get(cache_key) if _classification_cache else None
        if cached:
            resolved_wc, confidence, classifier_source = cached
        else:
            resolved_wc, confidence, classifier_source = _classify_heuristic(prompt, work_classes)
            if _classification_cache and resolved_wc:
                _classification_cache.put(cache_key, (resolved_wc, confidence, classifier_source))

    if not resolved_wc:
        resolved_wc = "simple"
        classifier_source = "lane_default"
        confidence = 0.0

    _emit("TRANSMISSION_WORK_CLASS_RESOLVED", req_id, _log, work_class=resolved_wc, source=classifier_source)
    _emit("TRANSMISSION_CONFIDENCE", req_id, _log, confidence=round(confidence, 3), threshold=confidence_threshold)

    gear_up = gear_up_on_low and confidence < confidence_threshold

    # --- Step 1: Build candidate chain (v1 logic) ---
    candidate_chain = _build_candidate_chain(
        resolved_wc, lane, models, lane_preferences, required_features, gear_up
    )
    _emit("TRANSMISSION_CHAIN_BUILT", req_id, _log,
          candidates=candidate_chain, work_class=resolved_wc, gear_up=gear_up)

    if not candidate_chain:
        _emit("TRANSMISSION_EXHAUSTED", req_id, _log,
              reason="no_candidates", work_class=resolved_wc)
        return {
            "status": "EXHAUSTED",
            "reason": "no_candidates",
            "work_class": resolved_wc,
            "req_id": req_id,
            "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    # --- Steps 2-5: Policy Resolver ---
    _h_path = Path(hypnos_state_path) if hypnos_state_path else None
    _r_path = Path(recall_state_path) if recall_state_path else None
    _ops_path = Path(ops_events_path) if ops_events_path else None

    recall_state  = _read_recall_state(_r_path, _log, req_id)
    hypnos_state  = _read_hypnos_state(_h_path, _log, req_id)

    policy_chain = list(candidate_chain)
    policy_applied = False
    recovery_context = False

    # Step 2: Apply Recall restrictions
    policy_chain, recall_applied = _apply_recall_restrictions(
        policy_chain, recall_state, agent_id, models
    )
    if recall_applied:
        policy_applied = True
        recovery_context = True

    # Step 3: Apply Hypnos restrictions
    policy_chain, hypnos_applied = _apply_hypnos_restrictions(
        policy_chain, hypnos_state, models, required_features
    )
    if hypnos_applied:
        policy_applied = True

    # Step 4: Re-rank remaining candidates (order is preserved from steps 2-3;
    # within each policy tier the v1 relative order is already correct)

    # Step 5: Emit TRANSMISSION_POLICY_REDUCED if chain changed
    if policy_applied:
        _emit("TRANSMISSION_POLICY_REDUCED", req_id, _log,
              original=candidate_chain, reduced=policy_chain,
              recall_applied=recall_applied, hypnos_applied=hypnos_applied)

    # Exhaustion after policy filtering
    if not policy_chain:
        _emit("TRANSMISSION_EXHAUSTED", req_id, _log,
              reason="policy_exhausted", work_class=resolved_wc,
              policy_active=True)
        return {
            "status": "EXHAUSTED",
            "reason": "policy_exhausted",
            "work_class": resolved_wc,
            "req_id": req_id,
            "policy_active": True,
            "duration_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    # Step 6: Select first candidate
    selected_model = policy_chain[0]
    model_cfg = models[selected_model]
    gear = model_cfg.get("tier", "unknown")

    _emit("TRANSMISSION_GEAR_SELECTED", req_id, _log,
          model=selected_model, gear=gear, lane=lane)

    # Execution config with capability masking (v1 unchanged)
    execution_config = _build_execution_config(resolved_wc, model_cfg)
    _emit("TRANSMISSION_EXECUTION_CONFIG", req_id, _log, execution_config=execution_config)

    duration_ms = round((time.perf_counter() - t0) * 1000, 2)
    _emit("TRANSMISSION_SUCCESS", req_id, _log, duration_ms=duration_ms, model=selected_model)

    # Ops bus emission (best-effort, never blocks)
    _emit_ops_event(
        req_id=req_id,
        model=selected_model,
        work_class=resolved_wc,
        gear=gear,
        policy_active=policy_applied,
        recovery_context=recovery_context,
        duration_ms=duration_ms,
        ops_path=_ops_path,
    )

    return {
        "model": selected_model,
        "provider": model_cfg.get("provider", ""),
        "candidate_chain": policy_chain,
        "work_class": resolved_wc,
        "confidence": round(confidence, 3),
        "classifier_source": classifier_source,
        "execution_config": execution_config,
        "gear": gear,
        "duration_ms": duration_ms,
        "req_id": req_id,
        "policy_active": policy_applied,
        "recovery_context": recovery_context,
    }


# ---------------------------------------------------------------------------
# CLI entry point (for manual testing)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "write some python code"
    result = route_with_transmission(prompt)
    print(json.dumps(result, indent=2))
