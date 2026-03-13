#!/usr/bin/env python3
"""
Unit tests for funnel_events.py — A-FUN-P3-001 weekly report export
TASK_ID: A-FUN-P3-001

Tests:
  1.  Empty rollup → INSUFFICIENT_DATA report, no crash
  2.  Missing key → graceful fallback to 0/False defaults
  3.  Interpretation: rc_runs_7d == 0 → "No RadCheck activity observed."
  4.  Interpretation: rc_runs_7d > 0 and attach_rate == 0 → "adoption opportunity"
  5.  Interpretation: rc_runs_7d > 0 and attach_rate > 0 → "within expected bounds"
  6.  Attach band: 0.0 → WATCH
  7.  Attach band: 0.3 → DEVELOPING
  8.  Attach band: 0.5 → HEALTHY
  9.  Expansion band: 0.03 → EARLY
  10. Expansion band: 0.08 → DEVELOPING
  11. Expansion band: 0.20 → STRONG
  12. Operator notes: sentinel not enabled + recommended > 0 → Sentinel review
  13. Operator notes: high views, zero expansions → positioning review
  14. Operator notes: healthy state → no blockers
  15. Report contains all 3 section headers
  16. Report contains all 7 executive metrics
  17. Decimal values formatted to 3 places
  18. Determinism: same rollup → byte-identical content twice
  19. write_weekly_report creates readable file
  20. emit_weekly_report_event appends FUNNEL_WEEKLY_REPORT to ops log
  21. report_hash in event matches sha256 of md content
  22. Language is observational (no "autonomous" claims in report body)
  23. System hostname appears in header
  24. INSUFFICIENT_DATA when rollup has no keys

Usage:
  python3 test_funnel_report.py
"""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from funnel_events import (  # noqa: E402
    render_weekly_report,
    write_weekly_report,
    emit_weekly_report_event,
    generate_weekly_report,
    _attach_band,
    _expansion_band,
    _funnel_interpretation,
    _operator_notes,
    EVT_WEEKLY_REPORT,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _full_rollup(**overrides) -> dict:
    base = {
        "window_days":             7,
        "radcheck_runs_7d":        5,
        "sentinel_recommended_7d": 3,
        "sentinel_enabled_present": True,
        "agent911_views_7d":       20,
        "agent911_expansions_7d":  4,
        "sentinel_attach_rate":    1.0,
        "agent911_expansion_rate": 0.2,
        "ts":                      _ts(),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInsufficient(unittest.TestCase):
    """Tests 1, 24 — empty/missing rollup produces INSUFFICIENT_DATA."""

    def test_empty_rollup(self):
        content = render_weekly_report({})
        self.assertIn("INSUFFICIENT_DATA", content)

    def test_no_crash_on_empty(self):
        # Must not raise
        try:
            render_weekly_report({})
        except Exception as e:
            self.fail(f"render_weekly_report({{}}) raised {e}")


class TestInterpretation(unittest.TestCase):
    """Tests 3–5 — funnel interpretation line."""

    def test_no_radcheck_activity(self):
        result = _funnel_interpretation(rc7=0, attach=0.0)
        self.assertEqual(result, "No RadCheck activity observed.")

    def test_adoption_opportunity(self):
        result = _funnel_interpretation(rc7=3, attach=0.0)
        self.assertIn("adoption opportunity", result)

    def test_within_expected_bounds(self):
        result = _funnel_interpretation(rc7=3, attach=1.0)
        self.assertIn("expected bounds", result)


class TestAttachBand(unittest.TestCase):
    """Tests 6–8 — sentinel attach rate bands."""

    def test_zero_is_watch(self):
        self.assertEqual(_attach_band(0.0), "WATCH")

    def test_mid_is_developing(self):
        self.assertEqual(_attach_band(0.3), "DEVELOPING")
        self.assertEqual(_attach_band(0.001), "DEVELOPING")

    def test_high_is_healthy(self):
        self.assertEqual(_attach_band(0.5), "HEALTHY")
        self.assertEqual(_attach_band(1.0), "HEALTHY")


class TestExpansionBand(unittest.TestCase):
    """Tests 9–11 — expansion rate bands."""

    def test_low_is_early(self):
        self.assertEqual(_expansion_band(0.03), "EARLY")
        self.assertEqual(_expansion_band(0.0), "EARLY")

    def test_mid_is_developing(self):
        self.assertEqual(_expansion_band(0.08), "DEVELOPING")
        self.assertEqual(_expansion_band(0.05), "DEVELOPING")

    def test_high_is_strong(self):
        self.assertEqual(_expansion_band(0.20), "STRONG")
        self.assertEqual(_expansion_band(0.15), "STRONG")


class TestOperatorNotes(unittest.TestCase):
    """Tests 12–14 — operator notes."""

    def test_sentinel_review_when_not_enabled_but_recommended(self):
        note = _operator_notes(sen_enabled=False, sen_rec_7d=3,
                               a9_views_7d=5, a9_exp_7d=1)
        self.assertIn("Sentinel", note)
        self.assertIn("review", note.lower())

    def test_positioning_review_when_views_high_expansions_zero(self):
        note = _operator_notes(sen_enabled=True, sen_rec_7d=2,
                               a9_views_7d=10, a9_exp_7d=0)
        self.assertIn("positioning", note.lower())

    def test_no_blockers_healthy_state(self):
        note = _operator_notes(sen_enabled=True, sen_rec_7d=0,
                               a9_views_7d=3, a9_exp_7d=1)
        self.assertIn("No material funnel blockers", note)


class TestReportStructure(unittest.TestCase):
    """Tests 15–17, 22–23 — report structure and content."""

    def setUp(self):
        self.rollup = _full_rollup()
        self.content = render_weekly_report(self.rollup, hostname="testhost")

    def test_all_three_section_headers(self):
        self.assertIn("## 1. Executive Funnel Summary", self.content)
        self.assertIn("## 2. Conversion Health", self.content)
        self.assertIn("## 3. Operator Notes", self.content)

    def test_all_seven_executive_metrics(self):
        metrics = [
            "RadCheck runs (7d)",
            "Sentinel recommended (7d)",
            "Sentinel enabled (present)",
            "Sentinel attach rate",
            "Agent911 views (7d)",
            "Agent911 expansions (7d)",
            "Agent911 expansion rate",
        ]
        for m in metrics:
            self.assertIn(m, self.content, f"Missing metric: {m}")

    def test_decimal_formatted_to_3_places(self):
        # attach_rate = 1.0 → should appear as "1.000"
        self.assertIn("1.000", self.content)
        # expansion_rate = 0.2 → "0.200"
        self.assertIn("0.200", self.content)

    def test_observational_language(self):
        # Should not contain autonomy claims
        autonomous_phrases = ["automatically", "self-heal", "I will", "I have"]
        for phrase in autonomous_phrases:
            self.assertNotIn(phrase, self.content,
                             f"Found autonomy claim: '{phrase}'")
        # Must have observational disclaimer
        self.assertIn("Observational", self.content)

    def test_hostname_in_header(self):
        self.assertIn("testhost", self.content)


class TestDeterminism(unittest.TestCase):
    """Test 18 — same rollup → byte-identical content."""

    def test_byte_identical_twice(self):
        rollup = _full_rollup()
        c1 = render_weekly_report(rollup, hostname="testhost")
        c2 = render_weekly_report(rollup, hostname="testhost")
        self.assertEqual(c1, c2, "Content differs between two renders")
        h1 = hashlib.sha256(c1.encode()).hexdigest()
        h2 = hashlib.sha256(c2.encode()).hexdigest()
        self.assertEqual(h1, h2, f"SHA256 differs: {h1} vs {h2}")

    def test_different_rollup_ts_produces_different_hash(self):
        r1 = _full_rollup(ts="2026-01-01T00:00:00Z")
        r2 = _full_rollup(ts="2026-01-02T00:00:00Z")
        c1 = render_weekly_report(r1, hostname="testhost")
        c2 = render_weekly_report(r2, hostname="testhost")
        h1 = hashlib.sha256(c1.encode()).hexdigest()
        h2 = hashlib.sha256(c2.encode()).hexdigest()
        self.assertNotEqual(h1, h2, "Different ts should produce different hash")


class TestWriteReport(unittest.TestCase):
    """Test 19 — write_weekly_report creates readable file."""

    def test_creates_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            path = f.name
        try:
            rollup = _full_rollup()
            content = render_weekly_report(rollup, hostname="testhost")
            ok = write_weekly_report(content, path)
            self.assertTrue(ok)
            with open(path, encoding="utf-8") as fh:
                read_back = fh.read()
            self.assertEqual(content, read_back)
        finally:
            os.unlink(path)


class TestEmitEvent(unittest.TestCase):
    """Tests 20–21 — emit_weekly_report_event."""

    def test_appends_correct_event(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            rollup = _full_rollup()
            content = render_weekly_report(rollup, hostname="testhost")
            report_hash = hashlib.sha256(content.encode()).hexdigest()
            ok = emit_weekly_report_event(rollup, report_hash, path)
            self.assertTrue(ok)
            with open(path) as fh:
                rec = json.loads(fh.read().strip())
            self.assertEqual(rec["event"], EVT_WEEKLY_REPORT)
            self.assertEqual(rec["source"], "weekly_rollup")
            self.assertIn("report_hash", rec)
            self.assertIn("attach_rate", rec)
            self.assertIn("expansion_rate", rec)
        finally:
            os.unlink(path)

    def test_report_hash_matches_content(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            rollup = _full_rollup()
            content = render_weekly_report(rollup, hostname="testhost")
            expected_hash = hashlib.sha256(content.encode()).hexdigest()
            emit_weekly_report_event(rollup, expected_hash, path)
            with open(path) as fh:
                rec = json.loads(fh.read().strip())
            self.assertEqual(rec["report_hash"], expected_hash)
        finally:
            os.unlink(path)


class TestMissingKeys(unittest.TestCase):
    """Test 2 — missing keys gracefully default to 0/False."""

    def test_missing_numeric_keys_default_to_zero(self):
        # Only provide ts — all others should default
        rollup = {"ts": _ts(), "radcheck_runs_7d": 2}
        content = render_weekly_report(rollup, hostname="testhost")
        # Should not raise and should contain the report structure
        self.assertIn("Executive Funnel Summary", content)
        self.assertIn("2", content)   # rc7 = 2

    def test_fully_populated_rollup_produces_full_report(self):
        rollup = _full_rollup()
        content = render_weekly_report(rollup, hostname="testhost")
        self.assertNotIn("INSUFFICIENT_DATA", content)
        self.assertIn("ACME Funnel Report", content)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
