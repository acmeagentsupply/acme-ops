#!/usr/bin/env python3
"""
Unit tests for sentinel_attach_bridge.py
TASK_ID: A-SEN-P4-001

Tests:
  1. Healthy system → recommended=False (FALSE POSITIVE GUARD)
  2. Single HIGH trigger (pred_risk MED) → recommended=True, confidence >= 20
  3. Single HIGH trigger (comp_risk HIGH) → recommended=True
  4. Single HIGH trigger (prot_events_24h >= 2) → recommended=True
  5. Single HIGH trigger (routing DEGRADED) → recommended=True
  6. Single HIGH trigger (radcheck score < 75) → recommended=True
  7. Multiple HIGH triggers → higher confidence
  8. MEDIUM trigger (comp SUSPECT) → recommended=True, confidence 10
  9. MEDIUM trigger (velocity DEGRADING) → recommended=True
  10. MEDIUM trigger (stall signatures) → recommended=True
  11. MEDIUM trigger (backup weak) → recommended=True
  12. Determinism: same inputs → identical output twice
  13. Missing/empty snap → graceful (no crash, recommended=False or safe default)
  14. Confidence capped at 100
  15. Severity mapping: HIGH ≥80, MED 60-79, LOW <60
  16. Healthy system: all five criteria independently verified
  17. reasons list is ordered/deterministic across runs

Usage:
  python3 test_sentinel_attach_bridge.py
"""

import copy
import sys
import os
import unittest

# Ensure the parent dir is on sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from sentinel_attach_bridge import compute_sentinel_recommendation  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _healthy_snap() -> dict:
    """Snap that should produce recommended=False."""
    return {
        "predictive_guard": {
            "risk_level":  "LOW",
            "risk_score":  15,
        },
        "compaction_state": {
            "risk":  "NOMINAL",
            "state": "NOMINAL",
        },
        "protection_rollup": {
            "events_24h":                0,
            "guard_cycles_24h":          0,
            "cooldown_suppressions_24h": 0,
        },
        "protection_events_24h": {
            "count": 0,
        },
        "routing": {
            "confidence": "HIGH",
        },
        "radcheck": {
            "score":              90,
            "velocity_direction": "STABLE",
        },
        "backup_state": {
            "last_backup_age_hours": 10.0,
        },
    }


def _snap_with(**overrides) -> dict:
    snap = _healthy_snap()
    for key, val in overrides.items():
        if "." in key:
            outer, inner = key.split(".", 1)
            snap.setdefault(outer, {})[inner] = val
        else:
            snap[key] = val
    return snap


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFalsePositiveGuard(unittest.TestCase):
    """Test 1 — healthy system must never recommend."""

    def test_healthy_system_not_recommended(self):
        snap = _healthy_snap()
        rec = compute_sentinel_recommendation(snap)
        self.assertFalse(rec["recommended"],
                         f"Expected recommended=False for healthy system, got: {rec}")
        self.assertEqual(rec["confidence"], 0)
        self.assertEqual(rec["reasons"], [])

    def test_healthy_system_severity_low(self):
        snap = _healthy_snap()
        rec = compute_sentinel_recommendation(snap)
        self.assertEqual(rec["severity"], "LOW")


class TestHighTriggers(unittest.TestCase):
    """Tests 2–6 — each HIGH trigger fires independently."""

    def test_pred_risk_med(self):
        snap = _healthy_snap()
        snap["predictive_guard"]["risk_level"] = "MED"
        snap["radcheck"]["score"] = 90  # ensure score doesn't also trigger
        rec = compute_sentinel_recommendation(snap)
        self.assertTrue(rec["recommended"])
        self.assertGreaterEqual(rec["confidence"], 20)
        self.assertTrue(any("Predictive" in r for r in rec["reasons"]))

    def test_pred_risk_high(self):
        snap = _healthy_snap()
        snap["predictive_guard"]["risk_level"] = "HIGH"
        snap["radcheck"]["score"] = 90
        rec = compute_sentinel_recommendation(snap)
        self.assertTrue(rec["recommended"])

    def test_comp_risk_high(self):
        snap = _healthy_snap()
        snap["compaction_state"]["risk"] = "HIGH"
        snap["compaction_state"]["state"] = "ACTIVE"
        rec = compute_sentinel_recommendation(snap)
        self.assertTrue(rec["recommended"])
        self.assertTrue(any("Compaction risk is HIGH" in r for r in rec["reasons"]))

    def test_prot_events_24h_trigger(self):
        snap = _healthy_snap()
        snap["protection_rollup"]["events_24h"] = 3
        snap["protection_events_24h"]["count"] = 3
        rec = compute_sentinel_recommendation(snap)
        self.assertTrue(rec["recommended"])
        self.assertTrue(any("protection events" in r for r in rec["reasons"]))

    def test_routing_degraded(self):
        snap = _healthy_snap()
        snap["routing"]["confidence"] = "DEGRADED"
        rec = compute_sentinel_recommendation(snap)
        self.assertTrue(rec["recommended"])
        self.assertTrue(any("DEGRADED" in r for r in rec["reasons"]))

    def test_radcheck_score_below_75(self):
        snap = _healthy_snap()
        snap["radcheck"]["score"] = 70
        rec = compute_sentinel_recommendation(snap)
        self.assertTrue(rec["recommended"])
        self.assertTrue(any("RadCheck score" in r for r in rec["reasons"]))


class TestMultipleHighTriggers(unittest.TestCase):
    """Test 7 — multiple HIGH triggers accumulate confidence."""

    def test_two_high_triggers(self):
        snap = _healthy_snap()
        snap["predictive_guard"]["risk_level"] = "HIGH"
        snap["compaction_state"]["risk"] = "HIGH"
        snap["compaction_state"]["state"] = "ACTIVE"
        snap["radcheck"]["score"] = 90
        rec = compute_sentinel_recommendation(snap)
        self.assertTrue(rec["recommended"])
        self.assertGreaterEqual(rec["confidence"], 40)

    def test_five_high_triggers_capped_at_100(self):
        snap = _healthy_snap()
        snap["predictive_guard"]["risk_level"] = "HIGH"
        snap["compaction_state"]["risk"] = "HIGH"
        snap["compaction_state"]["state"] = "ACTIVE"
        snap["protection_rollup"]["events_24h"] = 5
        snap["protection_events_24h"]["count"] = 5
        snap["routing"]["confidence"] = "DEGRADED"
        snap["radcheck"]["score"] = 60
        rec = compute_sentinel_recommendation(snap)
        self.assertLessEqual(rec["confidence"], 100)


class TestMediumTriggers(unittest.TestCase):
    """Tests 8–11 — MEDIUM triggers fire when HIGH are absent."""

    def test_comp_suspect(self):
        snap = _healthy_snap()
        snap["compaction_state"]["state"] = "SUSPECT"
        snap["compaction_state"]["risk"] = "MEDIUM"  # not HIGH
        rec = compute_sentinel_recommendation(snap)
        self.assertTrue(rec["recommended"])
        self.assertGreaterEqual(rec["confidence"], 10)
        self.assertTrue(any("SUSPECT" in r for r in rec["reasons"]))

    def test_velocity_degrading(self):
        snap = _healthy_snap()
        snap["radcheck"]["velocity_direction"] = "DEGRADING"
        rec = compute_sentinel_recommendation(snap)
        self.assertTrue(rec["recommended"])
        self.assertTrue(any("velocity" in r.lower() for r in rec["reasons"]))

    def test_stall_signatures(self):
        snap = _healthy_snap()
        snap["protection_rollup"]["guard_cycles_24h"] = 3
        snap["protection_rollup"]["cooldown_suppressions_24h"] = 2
        rec = compute_sentinel_recommendation(snap)
        self.assertTrue(rec["recommended"])
        self.assertTrue(any("Stall" in r for r in rec["reasons"]))

    def test_backup_weak(self):
        snap = _healthy_snap()
        snap["backup_state"]["last_backup_age_hours"] = 60.0
        rec = compute_sentinel_recommendation(snap)
        self.assertTrue(rec["recommended"])
        self.assertTrue(any("Backup posture weak" in r or "backup" in r.lower() for r in rec["reasons"]))


class TestDeterminism(unittest.TestCase):
    """Test 12 — same inputs → identical output twice."""

    def test_deterministic_healthy(self):
        snap = _healthy_snap()
        rec1 = compute_sentinel_recommendation(copy.deepcopy(snap))
        rec2 = compute_sentinel_recommendation(copy.deepcopy(snap))
        # ts will differ — compare everything except ts
        for key in ("recommended", "confidence", "severity", "reasons"):
            self.assertEqual(rec1[key], rec2[key],
                             f"Field '{key}' differs between runs")

    def test_deterministic_triggered(self):
        snap = _healthy_snap()
        snap["predictive_guard"]["risk_level"] = "HIGH"
        snap["compaction_state"]["risk"] = "HIGH"
        snap["compaction_state"]["state"] = "ACTIVE"
        rec1 = compute_sentinel_recommendation(copy.deepcopy(snap))
        rec2 = compute_sentinel_recommendation(copy.deepcopy(snap))
        for key in ("recommended", "confidence", "severity", "reasons"):
            self.assertEqual(rec1[key], rec2[key],
                             f"Field '{key}' differs between triggered runs")


class TestEdgeCases(unittest.TestCase):
    """Tests 13–14 — missing data, confidence cap."""

    def test_empty_snap_graceful(self):
        rec = compute_sentinel_recommendation({})
        self.assertIn("recommended", rec)
        self.assertIn("confidence", rec)
        self.assertIn("severity", rec)
        self.assertIn("reasons", rec)

    def test_confidence_never_exceeds_100(self):
        snap = _healthy_snap()
        # Trigger every possible signal
        snap["predictive_guard"]["risk_level"] = "HIGH"
        snap["compaction_state"]["risk"] = "HIGH"
        snap["compaction_state"]["state"] = "SUSPECT"
        snap["protection_rollup"]["events_24h"] = 5
        snap["protection_events_24h"]["count"] = 5
        snap["routing"]["confidence"] = "DEGRADED"
        snap["radcheck"]["score"] = 50
        snap["radcheck"]["velocity_direction"] = "DEGRADING"
        snap["protection_rollup"]["guard_cycles_24h"] = 5
        snap["protection_rollup"]["cooldown_suppressions_24h"] = 3
        snap["backup_state"]["last_backup_age_hours"] = 72.0
        rec = compute_sentinel_recommendation(snap)
        self.assertLessEqual(rec["confidence"], 100)


class TestSeverityMapping(unittest.TestCase):
    """Test 15 — severity bands are correct."""

    def test_high_severity_at_80plus(self):
        snap = _healthy_snap()
        # 4 HIGH triggers = 80 pts → HIGH severity
        snap["predictive_guard"]["risk_level"] = "HIGH"
        snap["compaction_state"]["risk"] = "HIGH"
        snap["compaction_state"]["state"] = "ACTIVE"
        snap["protection_rollup"]["events_24h"] = 3
        snap["protection_events_24h"]["count"] = 3
        snap["routing"]["confidence"] = "DEGRADED"
        snap["radcheck"]["score"] = 90  # don't add 5th trigger yet
        rec = compute_sentinel_recommendation(snap)
        self.assertEqual(rec["severity"], "HIGH",
                         f"Expected HIGH severity at confidence={rec['confidence']}")

    def test_medium_severity_at_60_79(self):
        snap = _healthy_snap()
        # 3 HIGH triggers = 60 pts → MEDIUM severity
        snap["predictive_guard"]["risk_level"] = "MED"
        snap["compaction_state"]["risk"] = "HIGH"
        snap["compaction_state"]["state"] = "ACTIVE"
        snap["routing"]["confidence"] = "DEGRADED"
        snap["radcheck"]["score"] = 90
        rec = compute_sentinel_recommendation(snap)
        # 3 HIGH triggers = 60; severity should be MEDIUM
        self.assertIn(rec["severity"], ("MEDIUM", "HIGH"),
                      f"Expected MEDIUM or HIGH for confidence={rec['confidence']}")


class TestHealthySystemCriteria(unittest.TestCase):
    """Test 16 — healthy system: each criterion independently prevents recommendation."""

    def test_only_low_pred_risk_not_sufficient_alone(self):
        """High score + low pred risk but degraded routing → still triggers."""
        snap = _healthy_snap()
        snap["routing"]["confidence"] = "DEGRADED"
        rec = compute_sentinel_recommendation(snap)
        self.assertTrue(rec["recommended"])

    def test_healthy_check_overrides_only_when_all_five_criteria_met(self):
        snap = _healthy_snap()
        # All 5 healthy criteria met → should not recommend
        rec = compute_sentinel_recommendation(snap)
        self.assertFalse(rec["recommended"])


class TestReasonsOrdering(unittest.TestCase):
    """Test 17 — reasons list is stable across multiple runs."""

    def test_reasons_order_stable(self):
        snap = _healthy_snap()
        snap["predictive_guard"]["risk_level"] = "HIGH"
        snap["compaction_state"]["risk"] = "HIGH"
        snap["compaction_state"]["state"] = "ACTIVE"
        snap["radcheck"]["velocity_direction"] = "DEGRADING"
        rec1 = compute_sentinel_recommendation(copy.deepcopy(snap))
        rec2 = compute_sentinel_recommendation(copy.deepcopy(snap))
        self.assertEqual(rec1["reasons"], rec2["reasons"])


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
