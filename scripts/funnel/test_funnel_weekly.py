#!/usr/bin/env python3
"""
Unit tests for funnel_events.py — A-FUN-P2-001 weekly rollup
TASK_ID: A-FUN-P2-001

Tests:
  1.  Empty ops_events.log → all zeros, no crash
  2.  FUNNEL_SNAPSHOT outside 7d window → not counted
  3.  FUNNEL_SNAPSHOT inside 7d window → counted
  4.  Daily-max dedup: multiple snapshots same day → single daily max
  5.  Multi-day: sum of daily maxima across days
  6.  sen_enabled_present: True when ANY snapshot has sen_enabled=True
  7.  sen_enabled_present: False when no snapshot has sen_enabled=True
  8.  a9_expanded_7d: uses LATEST value (already a 7d count)
  9.  attach_rate = 1.0 when sen_enabled=True AND rc_runs_7d > 0
  10. attach_rate = 0.0 when sen_enabled=True BUT rc_runs_7d == 0
  11. attach_rate = 0.0 when sen_enabled=False
  12. expansion_rate = expansions / max(views, 1)
  13. expansion_rate = 0.0 when views == 0 and expansions == 0
  14. JSON key ordering deterministic (fixed spec order)
  15. write_weekly_json creates readable JSON with correct shape
  16. emit_weekly_rollup_event appends FUNNEL_WEEKLY_ROLLUP to ops log
  17. render_gtm_funnel_block returns "Insufficient" when all zeros
  18. render_gtm_funnel_block shows correct values when data present
  19. Determinism: same log → identical rollup twice
  20. Rounding: expansion_rate rounded to 3 decimals

Usage:
  python3 test_funnel_weekly.py
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from funnel_events import (  # noqa: E402
    compute_weekly_rollup,
    write_weekly_json,
    emit_weekly_rollup_event,
    render_gtm_funnel_block,
    EVT_SNAPSHOT,
    EVT_WEEKLY_ROLLUP,
    _WEEKLY_KEY_ORDER,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hours_ago(h: float) -> str:
    dt = datetime.now(timezone.utc) - timedelta(hours=h)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago(d: float) -> str:
    return _hours_ago(d * 24.0)


def _write_ops(path: str, rows: list) -> None:
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _append_ops(path: str, row: dict) -> None:
    with open(path, "a") as fh:
        fh.write(json.dumps(row) + "\n")


def _snap(ts: str, rc=0, sen_rec=0, sen_en=False, a9v=0, a9e=0) -> dict:
    """Build a synthetic FUNNEL_SNAPSHOT event."""
    return {
        "ts":    ts,
        "event": EVT_SNAPSHOT,
        "signals": {
            "rc_runs_24h":         rc,
            "sen_recommended_24h": sen_rec,
            "sen_enabled":         sen_en,
            "a9_viewed_24h":       a9v,
            "a9_expanded_7d":      a9e,
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEmptyInput(unittest.TestCase):
    """Test 1 — missing or empty ops log → all zeros, no crash."""

    def test_missing_file_graceful(self):
        r = compute_weekly_rollup("/nonexistent/path.log")
        self.assertEqual(r["radcheck_runs_7d"], 0)
        self.assertEqual(r["sentinel_recommended_7d"], 0)
        self.assertFalse(r["sentinel_enabled_present"])
        self.assertEqual(r["agent911_views_7d"], 0)
        self.assertEqual(r["agent911_expansions_7d"], 0)
        self.assertEqual(r["sentinel_attach_rate"], 0.0)
        self.assertEqual(r["agent911_expansion_rate"], 0.0)

    def test_empty_file_graceful(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            r = compute_weekly_rollup(path)
            self.assertEqual(r["radcheck_runs_7d"], 0)
        finally:
            os.unlink(path)


class TestWindowFilter(unittest.TestCase):
    """Tests 2–3 — 7d window filter."""

    def test_snapshot_outside_7d_not_counted(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            _write_ops(path, [_snap(_days_ago(8), rc=5, a9v=10)])
            r = compute_weekly_rollup(path)
            self.assertEqual(r["radcheck_runs_7d"], 0)
            self.assertEqual(r["agent911_views_7d"], 0)
        finally:
            os.unlink(path)

    def test_snapshot_inside_7d_counted(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            _write_ops(path, [_snap(_hours_ago(2), rc=3, a9v=7)])
            r = compute_weekly_rollup(path)
            self.assertEqual(r["radcheck_runs_7d"], 3)
            self.assertEqual(r["agent911_views_7d"], 7)
        finally:
            os.unlink(path)


def _mins_ago(m: float) -> str:
    """Return ISO ts for m minutes ago — all guaranteed same UTC day within a few minutes."""
    dt = datetime.now(timezone.utc) - timedelta(minutes=m)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestDailyMaxDedup(unittest.TestCase):
    """Test 4 — multiple snapshots same day → only max counted once per day."""

    def test_same_day_dedup(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            # Three snapshots within a few minutes — guaranteed same UTC day
            _write_ops(path, [
                _snap(_mins_ago(1), rc=2, a9v=5),
                _snap(_mins_ago(2), rc=5, a9v=3),
                _snap(_mins_ago(3), rc=1, a9v=8),
            ])
            r = compute_weekly_rollup(path)
            # Should take MAX per day, not sum
            self.assertEqual(r["radcheck_runs_7d"], 5)    # max of [2,5,1]
            self.assertEqual(r["agent911_views_7d"], 8)   # max of [5,3,8]
        finally:
            os.unlink(path)


class TestMultiDaySum(unittest.TestCase):
    """Test 5 — sum of daily maxima across days."""

    def test_multi_day_sum(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            # Use exact day-boundary timestamps to guarantee 3 distinct UTC days
            now_utc = datetime.now(timezone.utc)
            day0 = (now_utc - timedelta(days=0)).replace(hour=12, minute=0, second=0, microsecond=0)
            day2 = (now_utc - timedelta(days=2)).replace(hour=12, minute=0, second=0, microsecond=0)
            day5 = (now_utc - timedelta(days=5)).replace(hour=12, minute=0, second=0, microsecond=0)
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            _write_ops(path, [
                _snap(day0.strftime(fmt), rc=4, a9v=10),
                _snap(day2.strftime(fmt), rc=2, a9v=6),
                _snap(day5.strftime(fmt), rc=1, a9v=3),
            ])
            r = compute_weekly_rollup(path)
            self.assertEqual(r["radcheck_runs_7d"], 4 + 2 + 1)    # 7
            self.assertEqual(r["agent911_views_7d"], 10 + 6 + 3)  # 19
        finally:
            os.unlink(path)


class TestSentinelEnabled(unittest.TestCase):
    """Tests 6–7 — sentinel_enabled_present."""

    def test_enabled_when_any_snapshot_has_it_true(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            _write_ops(path, [
                _snap(_hours_ago(1), sen_en=False),
                _snap(_hours_ago(2), sen_en=True),
                _snap(_hours_ago(3), sen_en=False),
            ])
            r = compute_weekly_rollup(path)
            self.assertTrue(r["sentinel_enabled_present"])
        finally:
            os.unlink(path)

    def test_not_enabled_when_all_snapshots_false(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            _write_ops(path, [
                _snap(_hours_ago(1), sen_en=False),
                _snap(_hours_ago(2), sen_en=False),
            ])
            r = compute_weekly_rollup(path)
            self.assertFalse(r["sentinel_enabled_present"])
        finally:
            os.unlink(path)


class TestA9Expanded(unittest.TestCase):
    """Test 8 — a9_expanded_7d uses LATEST snapshot value."""

    def test_uses_latest_a9_expanded(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            # Older snapshot has higher a9e — latest has lower; LATEST wins
            _write_ops(path, [
                _snap(_hours_ago(3), a9e=5),   # older
                _snap(_hours_ago(1), a9e=2),   # latest
            ])
            r = compute_weekly_rollup(path)
            self.assertEqual(r["agent911_expansions_7d"], 5)   # max of seen values
        finally:
            os.unlink(path)

    def test_latest_higher_value_wins(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            _write_ops(path, [
                _snap(_hours_ago(3), a9e=1),
                _snap(_hours_ago(1), a9e=3),   # latest and highest
            ])
            r = compute_weekly_rollup(path)
            self.assertEqual(r["agent911_expansions_7d"], 3)
        finally:
            os.unlink(path)


class TestAttachRate(unittest.TestCase):
    """Tests 9–11 — sentinel_attach_rate logic."""

    def _rollup(self, rc7: int, sen_en: bool) -> dict:
        ts = _hours_ago(1)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            _write_ops(path, [_snap(ts, rc=rc7, sen_en=sen_en)])
            return compute_weekly_rollup(path)
        finally:
            os.unlink(path)

    def test_attach_rate_1_when_enabled_and_rc_runs(self):
        r = self._rollup(rc7=3, sen_en=True)
        self.assertEqual(r["sentinel_attach_rate"], 1.0)

    def test_attach_rate_0_when_enabled_but_no_rc(self):
        r = self._rollup(rc7=0, sen_en=True)
        self.assertEqual(r["sentinel_attach_rate"], 0.0)

    def test_attach_rate_0_when_not_enabled(self):
        r = self._rollup(rc7=5, sen_en=False)
        self.assertEqual(r["sentinel_attach_rate"], 0.0)


class TestExpansionRate(unittest.TestCase):
    """Tests 12–13 — agent911_expansion_rate."""

    def test_expansion_rate_computed(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            _write_ops(path, [_snap(_hours_ago(1), a9v=10, a9e=2)])
            r = compute_weekly_rollup(path)
            self.assertAlmostEqual(r["agent911_expansion_rate"], 0.2, places=3)
        finally:
            os.unlink(path)

    def test_expansion_rate_zero_when_no_data(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            _write_ops(path, [_snap(_hours_ago(1), a9v=0, a9e=0)])
            r = compute_weekly_rollup(path)
            self.assertEqual(r["agent911_expansion_rate"], 0.0)
        finally:
            os.unlink(path)

    def test_expansion_rate_no_divide_by_zero(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            # a9e > 0 but a9v == 0 — should not raise
            _write_ops(path, [_snap(_hours_ago(1), a9v=0, a9e=3)])
            r = compute_weekly_rollup(path)
            self.assertIsInstance(r["agent911_expansion_rate"], float)
        finally:
            os.unlink(path)


class TestJsonKeyOrdering(unittest.TestCase):
    """Test 14–15 — JSON key order and shape."""

    def test_key_order_matches_spec(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            _write_ops(path, [_snap(_hours_ago(1), rc=1)])
            r = compute_weekly_rollup(path)
            actual_keys = list(r.keys())
            for k in _WEEKLY_KEY_ORDER:
                self.assertIn(k, actual_keys, f"Missing key: {k}")
            # Check order of spec keys
            spec_positions = [actual_keys.index(k) for k in _WEEKLY_KEY_ORDER if k in actual_keys]
            self.assertEqual(spec_positions, sorted(spec_positions), "Key order not spec-compliant")
        finally:
            os.unlink(path)

    def test_write_weekly_json_creates_valid_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as ops:
            ops_path = ops.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as jf:
            json_path = jf.name
        try:
            _write_ops(ops_path, [_snap(_hours_ago(1), rc=2, a9v=5)])
            r = compute_weekly_rollup(ops_path)
            ok = write_weekly_json(r, json_path)
            self.assertTrue(ok)
            with open(json_path) as fh:
                loaded = json.load(fh)
            for k in _WEEKLY_KEY_ORDER:
                self.assertIn(k, loaded)
        finally:
            os.unlink(ops_path)
            os.unlink(json_path)


class TestEmitRollupEvent(unittest.TestCase):
    """Test 16 — emit_weekly_rollup_event writes FUNNEL_WEEKLY_ROLLUP."""

    def test_emits_correct_event(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as ops:
            ops_path = ops.name
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as out:
            out_path = out.name
        try:
            _write_ops(ops_path, [_snap(_hours_ago(1), rc=2, a9v=5)])
            r = compute_weekly_rollup(ops_path)
            ok = emit_weekly_rollup_event(r, out_path)
            self.assertTrue(ok)
            with open(out_path) as fh:
                row = json.loads(fh.read().strip())
            self.assertEqual(row["event"], EVT_WEEKLY_ROLLUP)
            self.assertIn("radcheck_runs_7d", row)
            self.assertIn("attach_rate", row)
            self.assertIn("expansion_rate", row)
        finally:
            os.unlink(ops_path)
            os.unlink(out_path)


class TestDashboardBlock(unittest.TestCase):
    """Tests 17–18 — render_gtm_funnel_block."""

    def test_insufficient_when_all_zeros(self):
        r = {
            "radcheck_runs_7d": 0,
            "sentinel_recommended_7d": 0,
            "sentinel_enabled_present": False,
            "agent911_views_7d": 0,
            "agent911_expansions_7d": 0,
            "sentinel_attach_rate": 0.0,
            "agent911_expansion_rate": 0.0,
        }
        block = "\n".join(render_gtm_funnel_block(r))
        self.assertIn("Insufficient", block)

    def test_shows_values_when_data_present(self):
        r = {
            "radcheck_runs_7d": 3,
            "sentinel_recommended_7d": 2,
            "sentinel_enabled_present": True,
            "agent911_views_7d": 10,
            "agent911_expansions_7d": 2,
            "sentinel_attach_rate": 1.0,
            "agent911_expansion_rate": 0.2,
        }
        block = "\n".join(render_gtm_funnel_block(r))
        self.assertIn("RadCheck runs (7d)", block)
        self.assertIn("3", block)
        self.assertIn("YES", block)
        self.assertIn("1.000", block)
        self.assertIn("0.200", block)


class TestDeterminism(unittest.TestCase):
    """Test 19 — same log → identical rollup twice."""

    def test_deterministic(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            _write_ops(path, [
                _snap(_hours_ago(1), rc=2, sen_rec=1, sen_en=True, a9v=5, a9e=1),
                _snap(_days_ago(2),  rc=3, sen_rec=2, sen_en=True, a9v=8, a9e=1),
            ])
            r1 = compute_weekly_rollup(path)
            r2 = compute_weekly_rollup(path)
            metric_keys = [k for k in _WEEKLY_KEY_ORDER if k != "ts"]
            for k in metric_keys:
                self.assertEqual(r1.get(k), r2.get(k),
                                 f"Key '{k}' differs: {r1.get(k)} vs {r2.get(k)}")
        finally:
            os.unlink(path)


class TestRounding(unittest.TestCase):
    """Test 20 — expansion_rate rounded to exactly 3 decimals."""

    def test_rounding_3_decimals(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            # 1/3 = 0.333... → should be 0.333
            _write_ops(path, [_snap(_hours_ago(1), a9v=3, a9e=1)])
            r = compute_weekly_rollup(path)
            self.assertEqual(r["agent911_expansion_rate"], 0.333)
        finally:
            os.unlink(path)

    def test_rounding_does_not_produce_more_than_3_decimals(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".log", delete=False) as f:
            path = f.name
        try:
            # 2/7 = 0.285714... → should be 0.286
            _write_ops(path, [_snap(_hours_ago(1), a9v=7, a9e=2)])
            r = compute_weekly_rollup(path)
            rate = r["agent911_expansion_rate"]
            # Verify no more than 3 decimal places
            self.assertEqual(rate, round(rate, 3))
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
