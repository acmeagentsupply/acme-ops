#!/usr/bin/env python3
"""
Unit tests for sentinel_funnel_alignment.py
TASK_ID: A-SEN-P4-002

Tests:
  1.  recommended=T, enabled=T  → CONSISTENT
  2.  recommended=T, enabled=F  → EXPECTED_PRESSURE
  3.  recommended=F, enabled=T  → LEGACY_ENABLE
  4.  recommended=F, enabled=F  → CONSISTENT
  5.  Both sources missing      → DRIFT
  6.  Only a9_state missing     → state derived from funnel only (partial)
  7.  Only funnel missing       → state derived from a9_state only (partial)
  8.  Partial data: confidence reduced by 20
  9.  CONSISTENT confidence = 90
  10. EXPECTED_PRESSURE confidence = 75
  11. LEGACY_ENABLE confidence = 60
  12. DRIFT confidence = 0
  13. Determinism: same inputs → identical alignment_state twice
  14. emit_alignment_event appends SENTINEL_FUNNEL_ALIGNMENT to ops log
  15. Dashboard block: state in first line
  16. Dashboard block: shows advisory note on EXPECTED_PRESSURE/LEGACY_ENABLE
  17. Dashboard block: no note on CONSISTENT
  18. Non-boolean truthy values handled correctly
  19. Zero-confidence DRIFT on exception

Usage:
  python3 test_sentinel_funnel_alignment.py
"""

import copy
import json
import os
import sys
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from sentinel_funnel_alignment import (  # noqa: E402
    compute_alignment,
    emit_alignment_event,
    render_alignment_block,
    CONSISTENT,
    EXPECTED_PRESSURE,
    LEGACY_ENABLE,
    DRIFT,
    EVT_ALIGNMENT,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _a9(recommended: bool) -> dict:
    return {"sentinel_recommendation": {"recommended": recommended}}


def _funnel(enabled_present: bool) -> dict:
    return {"sentinel_enabled_present": enabled_present}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestStateMatrix(unittest.TestCase):
    """Tests 1–5 — alignment state matrix."""

    def test_T_T_consistent(self):
        r = compute_alignment(_a9(True), _funnel(True))
        self.assertEqual(r["alignment_state"], CONSISTENT)

    def test_T_F_expected_pressure(self):
        r = compute_alignment(_a9(True), _funnel(False))
        self.assertEqual(r["alignment_state"], EXPECTED_PRESSURE)

    def test_F_T_legacy_enable(self):
        r = compute_alignment(_a9(False), _funnel(True))
        self.assertEqual(r["alignment_state"], LEGACY_ENABLE)

    def test_F_F_consistent(self):
        r = compute_alignment(_a9(False), _funnel(False))
        self.assertEqual(r["alignment_state"], CONSISTENT)

    def test_both_missing_drift(self):
        r = compute_alignment({}, {})
        self.assertEqual(r["alignment_state"], DRIFT)


class TestPartialData(unittest.TestCase):
    """Tests 6–8 — partial source handling."""

    def test_only_funnel_missing_uses_a9_state(self):
        # a9 says recommended=True; funnel missing → enabled_present=False (conservative)
        r = compute_alignment(_a9(True), {})
        # recommended=T, enabled=F → EXPECTED_PRESSURE
        self.assertEqual(r["alignment_state"], EXPECTED_PRESSURE)

    def test_only_a9_missing_uses_funnel(self):
        # funnel says enabled=True; a9 missing → recommended=False (conservative)
        r = compute_alignment({}, _funnel(True))
        # recommended=F, enabled=T → LEGACY_ENABLE
        self.assertEqual(r["alignment_state"], LEGACY_ENABLE)

    def test_partial_confidence_reduced(self):
        # Full CONSISTENT = 90; partial EXPECTED_PRESSURE(75) - 20 = 55
        full = compute_alignment(_a9(True), _funnel(True))
        partial = compute_alignment(_a9(True), {})  # funnel missing → EXPECTED_PRESSURE
        self.assertEqual(full["confidence"], 90)
        self.assertLess(partial["confidence"], 75)       # below full EP confidence
        self.assertEqual(partial["confidence"], 75 - 20) # 55


class TestConfidence(unittest.TestCase):
    """Tests 9–12 — confidence values per state."""

    def test_consistent_confidence_90(self):
        r = compute_alignment(_a9(True), _funnel(True))
        self.assertEqual(r["confidence"], 90)

    def test_expected_pressure_confidence_75(self):
        r = compute_alignment(_a9(True), _funnel(False))
        self.assertEqual(r["confidence"], 75)

    def test_legacy_enable_confidence_60(self):
        r = compute_alignment(_a9(False), _funnel(True))
        self.assertEqual(r["confidence"], 60)

    def test_drift_confidence_0(self):
        r = compute_alignment({}, {})
        self.assertEqual(r["confidence"], 0)


class TestDeterminism(unittest.TestCase):
    """Test 13 — same inputs → identical result."""

    def test_deterministic(self):
        a9 = _a9(True)
        fu = _funnel(True)
        r1 = compute_alignment(copy.deepcopy(a9), copy.deepcopy(fu))
        r2 = compute_alignment(copy.deepcopy(a9), copy.deepcopy(fu))
        for key in ("alignment_state", "recommended", "enabled_present", "confidence"):
            self.assertEqual(r1[key], r2[key],
                             f"Field '{key}' differs: {r1[key]} vs {r2[key]}")

    def test_all_four_states_deterministic(self):
        cases = [
            (_a9(True),  _funnel(True),  CONSISTENT),
            (_a9(True),  _funnel(False), EXPECTED_PRESSURE),
            (_a9(False), _funnel(True),  LEGACY_ENABLE),
            (_a9(False), _funnel(False), CONSISTENT),
        ]
        for a9, fu, expected in cases:
            r1 = compute_alignment(a9, fu)
            r2 = compute_alignment(a9, fu)
            self.assertEqual(r1["alignment_state"], expected)
            self.assertEqual(r1["alignment_state"], r2["alignment_state"])


class TestEmitEvent(unittest.TestCase):
    """Test 14 — emit_alignment_event appends SENTINEL_FUNNEL_ALIGNMENT."""

    def test_appends_correct_event(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            alignment = compute_alignment(_a9(True), _funnel(False))
            ok = emit_alignment_event(alignment, path)
            self.assertTrue(ok)
            with open(path) as fh:
                rec = json.loads(fh.read().strip())
            self.assertEqual(rec["event"], EVT_ALIGNMENT)
            self.assertEqual(rec["alignment_state"], EXPECTED_PRESSURE)
            self.assertIn("recommended", rec)
            self.assertIn("enabled_present", rec)
            self.assertIn("confidence", rec)
        finally:
            os.unlink(path)


class TestDashboardBlock(unittest.TestCase):
    """Tests 15–17 — render_alignment_block."""

    def test_state_in_first_line(self):
        al = compute_alignment(_a9(True), _funnel(True))
        block = render_alignment_block(al)
        self.assertIn(CONSISTENT, block[0])

    def test_note_shown_on_expected_pressure(self):
        al = compute_alignment(_a9(True), _funnel(False))
        block = "\n".join(render_alignment_block(al))
        self.assertIn("Note", block)

    def test_note_shown_on_legacy_enable(self):
        al = compute_alignment(_a9(False), _funnel(True))
        block = "\n".join(render_alignment_block(al))
        self.assertIn("Note", block)

    def test_no_note_on_consistent(self):
        al = compute_alignment(_a9(True), _funnel(True))
        block = "\n".join(render_alignment_block(al))
        self.assertNotIn("Note", block)


class TestEdgeCases(unittest.TestCase):
    """Tests 18–19 — edge cases."""

    def test_truthy_non_boolean_values(self):
        # Integers and strings as bool-convertible values
        a9  = {"sentinel_recommendation": {"recommended": 1}}
        fu  = {"sentinel_enabled_present": 0}
        r   = compute_alignment(a9, fu)
        # recommended=True (1), enabled_present=False (0) → EXPECTED_PRESSURE
        self.assertEqual(r["alignment_state"], EXPECTED_PRESSURE)

    def test_none_recommended_treated_as_false(self):
        a9  = {"sentinel_recommendation": {"recommended": None}}
        fu  = {"sentinel_enabled_present": True}
        r   = compute_alignment(a9, fu)
        # None treated as missing → conservative False → LEGACY_ENABLE
        self.assertEqual(r["alignment_state"], LEGACY_ENABLE)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
