#!/usr/bin/env python3
"""
Unit tests for funnel_events.py
TASK_ID: A-FUN-P1-001

Tests:
  1.  Empty/missing paths → graceful (all counts 0/False, no crash)
  2.  RADCHECK_RUN: count from ndjson within 24h window
  3.  RADCHECK_RUN: entries outside 24h not counted
  4.  SENTINEL_RECOMMENDED: counts only recommended=true events
  5.  SENTINEL_RECOMMENDED: extra_filter excludes recommended=false
  6.  SENTINEL_ENABLED: returns True when protection events present in 7d
  7.  SENTINEL_ENABLED: returns False when no events
  8.  AGENT911_VIEWED: count from ndjson within 24h window
  9.  AGENT911_VIEWED: entries outside 24h not counted
  10. Confidence = 0 when count = 0
  11. Confidence > 0 when count >= 1
  12. Confidence bounded [0, 100]
  13. Dashboard block: contains expected keys/labels
  14. Determinism: compute_funnel_signals called twice → identical signal counts
  15. emit_funnel_events writes FUNNEL_SNAPSHOT to output file
  16. emit_funnel_events writes transition event only when count increases
  17. No event written if count unchanged from state
  18. funnel_state.json updated after emit

Usage:
  python3 test_funnel_events.py
"""

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from funnel_events import (  # noqa: E402
    compute_funnel_signals,
    emit_funnel_events,
    render_funnel_block,
    _count_ndjson_in_window,
    _count_ops_events,
    _sentinel_enabled_7d,
    _confidence_for,
    EVT_SNAPSHOT,
    EVT_RC_RUN,
    EVT_SEN_REC,
    EVT_SEN_ENABLED,
    EVT_A9_VIEWED,
    EVT_A9_EXPANDED,
    SRC_RC_HIST,
    SRC_A9_HIST,
    SRC_OPS,
    SRC_FUNNEL_ST,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hours_ago(h: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=h)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_ndjson(path: str, rows: list) -> None:
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _read_ndjson(path: str) -> list:
    rows = []
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except Exception:
        pass
    return rows


# ---------------------------------------------------------------------------
# Test helpers for temp files
# ---------------------------------------------------------------------------

class FunnelTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def _path(self, name: str) -> str:
        return os.path.join(self.tmpdir, name)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestGracefulMissing(FunnelTestCase):
    """Test 1 — empty/missing files don't crash."""

    def test_count_ndjson_missing_file(self):
        result = _count_ndjson_in_window("/nonexistent/path.ndjson", 24.0)
        self.assertEqual(result, 0)

    def test_count_ops_events_missing_file(self):
        result = _count_ops_events("/nonexistent/ops.log", "SENTINEL_RECOMMENDATION_EVAL", 24.0)
        self.assertEqual(result, 0)

    def test_sentinel_enabled_missing_file(self):
        # Temporarily patch SRC_OPS to nonexistent
        import funnel_events as fe
        orig = fe.SRC_OPS
        fe.SRC_OPS = "/nonexistent/ops.log"
        try:
            enabled, count = _sentinel_enabled_7d()
            self.assertFalse(enabled)
            self.assertEqual(count, 0)
        finally:
            fe.SRC_OPS = orig


class TestRadCheckCount(FunnelTestCase):
    """Tests 2–3 — RADCHECK_RUN counting."""

    def test_counts_within_24h(self):
        path = self._path("rc.ndjson")
        _write_ndjson(path, [
            {"ts": _hours_ago(1), "score": 80},
            {"ts": _hours_ago(12), "score": 75},
        ])
        result = _count_ndjson_in_window(path, 24.0)
        self.assertEqual(result, 2)

    def test_excludes_entries_outside_24h(self):
        path = self._path("rc.ndjson")
        _write_ndjson(path, [
            {"ts": _hours_ago(25), "score": 80},   # outside window
            {"ts": _hours_ago(1),  "score": 75},   # inside window
        ])
        result = _count_ndjson_in_window(path, 24.0)
        self.assertEqual(result, 1)

    def test_zero_when_all_outside_window(self):
        path = self._path("rc.ndjson")
        _write_ndjson(path, [
            {"ts": _hours_ago(30), "score": 80},
            {"ts": _hours_ago(48), "score": 75},
        ])
        result = _count_ndjson_in_window(path, 24.0)
        self.assertEqual(result, 0)


class TestSentinelRecommendedCount(FunnelTestCase):
    """Tests 4–5 — SENTINEL_RECOMMENDED counting with extra_filter."""

    def test_counts_only_recommended_true(self):
        path = self._path("ops.log")
        with open(path, "w") as fh:
            fh.write(json.dumps({
                "ts": _hours_ago(1),
                "event": "SENTINEL_RECOMMENDATION_EVAL",
                "recommended": True,
            }) + "\n")
            fh.write(json.dumps({
                "ts": _hours_ago(2),
                "event": "SENTINEL_RECOMMENDATION_EVAL",
                "recommended": False,
            }) + "\n")
        result = _count_ops_events(
            path, "SENTINEL_RECOMMENDATION_EVAL", 24.0,
            extra_filter={"recommended": True},
        )
        self.assertEqual(result, 1)

    def test_excludes_recommended_false(self):
        path = self._path("ops.log")
        with open(path, "w") as fh:
            fh.write(json.dumps({
                "ts": _hours_ago(1),
                "event": "SENTINEL_RECOMMENDATION_EVAL",
                "recommended": False,
            }) + "\n")
        result = _count_ops_events(
            path, "SENTINEL_RECOMMENDATION_EVAL", 24.0,
            extra_filter={"recommended": True},
        )
        self.assertEqual(result, 0)


class TestSentinelEnabled(FunnelTestCase):
    """Tests 6–7 — SENTINEL_ENABLED detection."""

    def test_enabled_when_protection_events_present(self):
        import funnel_events as fe
        orig = fe.SRC_OPS
        path = self._path("ops.log")
        with open(path, "w") as fh:
            fh.write(json.dumps({
                "ts": _hours_ago(2),
                "event": "SENTINEL_PROTECTION_KICKSTART",
            }) + "\n")
        fe.SRC_OPS = path
        try:
            enabled, count = _sentinel_enabled_7d()
            self.assertTrue(enabled)
            self.assertEqual(count, 1)
        finally:
            fe.SRC_OPS = orig

    def test_not_enabled_when_no_events(self):
        import funnel_events as fe
        orig = fe.SRC_OPS
        path = self._path("ops.log")
        with open(path, "w") as fh:
            fh.write(json.dumps({
                "ts": _hours_ago(2),
                "event": "SOME_OTHER_EVENT",
            }) + "\n")
        fe.SRC_OPS = path
        try:
            enabled, count = _sentinel_enabled_7d()
            self.assertFalse(enabled)
            self.assertEqual(count, 0)
        finally:
            fe.SRC_OPS = orig


class TestAgent911ViewedCount(FunnelTestCase):
    """Tests 8–9 — AGENT911_VIEWED counting."""

    def test_counts_within_24h(self):
        path = self._path("a9.ndjson")
        _write_ndjson(path, [
            {"ts": _hours_ago(0.5), "snapshot_ms": 50},
            {"ts": _hours_ago(10),  "snapshot_ms": 60},
        ])
        result = _count_ndjson_in_window(path, 24.0)
        self.assertEqual(result, 2)

    def test_excludes_entries_outside_24h(self):
        path = self._path("a9.ndjson")
        _write_ndjson(path, [
            {"ts": _hours_ago(26), "snapshot_ms": 50},
            {"ts": _hours_ago(5),  "snapshot_ms": 60},
        ])
        result = _count_ndjson_in_window(path, 24.0)
        self.assertEqual(result, 1)


class TestConfidence(FunnelTestCase):
    """Tests 10–12 — confidence scoring."""

    def test_zero_count_gives_zero_confidence(self):
        self.assertEqual(_confidence_for(0), 0)

    def test_positive_count_gives_positive_confidence(self):
        self.assertGreater(_confidence_for(1), 0)

    def test_confidence_bounded(self):
        for count in (0, 1, 5, 100, 1000):
            conf = _confidence_for(count)
            self.assertGreaterEqual(conf, 0)
            self.assertLessEqual(conf, 100)


class TestDashboardBlock(FunnelTestCase):
    """Test 13 — dashboard block contains expected content."""

    def test_block_contains_key_labels(self):
        sig = {
            "rc_runs_24h":          3,
            "sen_recommended_24h":  2,
            "sen_enabled":          True,
            "a9_viewed_24h":        5,
            "a9_expanded_7d":       1,
        }
        block = render_funnel_block(sig)
        full = "\n".join(block)
        self.assertIn("RadCheck runs (24h)", full)
        self.assertIn("Sentinel recommended (24h)", full)
        self.assertIn("Agent911 expansions (7d)", full)
        self.assertIn("3", full)
        self.assertIn("2", full)
        self.assertIn("YES", full)  # sen_enabled=True


class TestDeterminism(FunnelTestCase):
    """Test 14 — compute_funnel_signals twice → identical counts (pure read)."""

    def test_pure_read_deterministic(self):
        # compute_funnel_signals is pure read — calling it twice without
        # modifying any files should yield identical results.
        sig1 = compute_funnel_signals()
        sig2 = compute_funnel_signals()
        keys = [
            "rc_runs_24h",
            "sen_recommended_24h",
            "sen_enabled",
            "a9_viewed_24h",
            "a9_expanded_7d",
        ]
        for k in keys:
            self.assertEqual(sig1.get(k), sig2.get(k),
                             f"Signal '{k}' differs between runs: {sig1.get(k)} vs {sig2.get(k)}")


class TestEmitFunnelEvents(FunnelTestCase):
    """Tests 15–18 — emit_funnel_events behavior."""

    def _make_signals(self, rc=2, sen_rec=1, sen_en=True, a9=3, a9_exp=1) -> dict:
        return {
            "rc_runs_24h":              rc,
            "rc_confidence":            60,
            "sen_recommended_24h":      sen_rec,
            "sen_recommended_confidence": 70,
            "sen_enabled":              sen_en,
            "sen_prot_events_7d":       5,
            "sen_enabled_confidence":   80,
            "a9_viewed_24h":            a9,
            "a9_viewed_confidence":     80,
            "a9_expanded_7d":           a9_exp,
            "a9_expanded_source":       "ops_events",
            "a9_expanded_confidence":   70,
        }

    def test_always_emits_funnel_snapshot(self):
        ops = self._path("ops.log")
        state = self._path("state.json")
        import funnel_events as fe
        orig_ops, orig_st = fe.SRC_FUNNEL_ST, fe.SRC_FUNNEL_ST
        fe.SRC_FUNNEL_ST = state
        try:
            emitted = emit_funnel_events(self._make_signals(), ops_path=ops)
            self.assertIn(EVT_SNAPSHOT, emitted)
            rows = _read_ndjson(ops)
            self.assertTrue(any(r.get("event") == EVT_SNAPSHOT for r in rows))
        finally:
            fe.SRC_FUNNEL_ST = orig_ops

    def test_transition_event_on_count_increase(self):
        ops = self._path("ops.log")
        state = self._path("state.json")
        import funnel_events as fe
        orig = fe.SRC_FUNNEL_ST
        fe.SRC_FUNNEL_ST = state
        try:
            # First emit — no prior state → all counts "increased" from 0
            signals = self._make_signals(rc=2)
            emitted = emit_funnel_events(signals, ops_path=ops)
            self.assertIn(EVT_RC_RUN, emitted)
        finally:
            fe.SRC_FUNNEL_ST = orig

    def test_no_transition_event_if_count_unchanged(self):
        ops = self._path("ops.log")
        state = self._path("state.json")
        import funnel_events as fe
        orig = fe.SRC_FUNNEL_ST
        fe.SRC_FUNNEL_ST = state
        try:
            signals = self._make_signals(rc=2, sen_rec=1, a9=3, a9_exp=1)
            # First emit — sets state
            emit_funnel_events(signals, ops_path=ops)
            # Clear ops log (to see only second-run events)
            open(ops, "w").close()
            # Second emit — same signals → no transition events
            emitted2 = emit_funnel_events(signals, ops_path=ops)
            rows2 = _read_ndjson(ops)
            event_names2 = [r.get("event") for r in rows2]
            # Only FUNNEL_SNAPSHOT should appear (not individual transitions)
            for transition_evt in (EVT_RC_RUN, EVT_SEN_REC, EVT_SEN_ENABLED,
                                   EVT_A9_VIEWED, EVT_A9_EXPANDED):
                self.assertNotIn(transition_evt, event_names2,
                                 f"Unexpected transition event {transition_evt} on unchanged count")
            self.assertIn(EVT_SNAPSHOT, event_names2)
        finally:
            fe.SRC_FUNNEL_ST = orig

    def test_funnel_state_updated_after_emit(self):
        ops = self._path("ops.log")
        state = self._path("state.json")
        import funnel_events as fe
        orig = fe.SRC_FUNNEL_ST
        fe.SRC_FUNNEL_ST = state
        try:
            signals = self._make_signals(rc=5)
            emit_funnel_events(signals, ops_path=ops)
            with open(state) as fh:
                saved = json.load(fh)
            self.assertEqual(saved.get("rc_runs_24h"), 5)
        finally:
            fe.SRC_FUNNEL_ST = orig


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
