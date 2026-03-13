#!/usr/bin/env python3
"""
test_lazarus_score.py — Unit tests for Lazarus scoring engine
stdlib only (unittest + importlib). No pytest required.

Run: python3 test_lazarus_score.py
Expected: all tests PASS, exit 0
"""

import sys
import os
import unittest

# Add lazarus dir to path for import
sys.path.insert(0, os.path.dirname(__file__))

from lazarus import compute_score, CHECK_WEIGHTS, _risk_band


def _make_findings(overrides: dict = None) -> list:
    """
    Build a full findings list where all checks PASS by default.
    overrides: dict of check_id -> bool (passed value)
    """
    base = {check_id: True for check_id in CHECK_WEIGHTS}
    if overrides:
        base.update(overrides)
    return [
        {
            "check_id": check_id,
            "passed": passed,
            "severity": "HIGH",
            "evidence": f"test evidence for {check_id}",
            "remediation": f"fix {check_id}",
            "confidence": 1.0,
        }
        for check_id, passed in base.items()
    ]


class TestRiskBand(unittest.TestCase):
    def test_low(self):
        self.assertEqual(_risk_band(100), "LOW")
        self.assertEqual(_risk_band(80), "LOW")

    def test_moderate(self):
        self.assertEqual(_risk_band(79), "MODERATE")
        self.assertEqual(_risk_band(60), "MODERATE")

    def test_high(self):
        self.assertEqual(_risk_band(59), "HIGH")
        self.assertEqual(_risk_band(40), "HIGH")

    def test_critical(self):
        self.assertEqual(_risk_band(39), "CRITICAL")
        self.assertEqual(_risk_band(0), "CRITICAL")


class TestWeightSum(unittest.TestCase):
    def test_weights_sum_to_100(self):
        """CHECK_WEIGHTS must sum to exactly 100."""
        total = sum(CHECK_WEIGHTS.values())
        self.assertEqual(total, 100, f"Weights sum to {total}, expected 100")

    def test_all_weights_positive(self):
        for k, v in CHECK_WEIGHTS.items():
            self.assertGreater(v, 0, f"{k} weight must be > 0")


class TestComputeScore(unittest.TestCase):
    def test_all_pass_gives_100(self):
        """All checks passing → score = 100."""
        findings = _make_findings()
        score, risk, top5, failed = compute_score(findings)
        self.assertEqual(score, 100)
        self.assertEqual(risk, "LOW")
        self.assertEqual(len(failed), 0)

    def test_empty_findings_gives_0(self):
        """Empty findings list → score = 0, risk = CRITICAL."""
        score, risk, top5, failed = compute_score([])
        self.assertEqual(score, 0)
        self.assertEqual(risk, "CRITICAL")
        self.assertEqual(len(failed), len(CHECK_WEIGHTS))

    def test_no_backup_scenario(self):
        """
        No backup scenario: LZ_SURF_001 (20pts) + LZ_SURF_002 (10pts) +
        LZ_CLD_001 (5pts) fail. Score should be 100 - 35 = 65 (MODERATE).
        """
        findings = _make_findings({
            "LZ_SURF_001": False,  # ~/.openclaw/ not covered (-20)
            "LZ_SURF_002": False,  # watchdog/ not covered (-10)
            "LZ_CLD_001": False,   # GDrive absent (-5)
        })
        score, risk, top5, failed = compute_score(findings)
        expected = 100 - 20 - 10 - 5
        self.assertEqual(score, expected)
        self.assertEqual(risk, "MODERATE")  # 65 = MODERATE

    def test_time_machine_missing(self):
        """
        TM not configured: LZ_TM_001 (-5) + LZ_TM_002 (-5) fail.
        Score = 90, not 100. TM failures must show up in score.
        """
        findings = _make_findings({
            "LZ_TM_001": False,  # TM not configured (-5)
            "LZ_TM_002": False,  # No recent TM backup (-5)
        })
        score, risk, top5, failed = compute_score(findings)
        self.assertEqual(score, 90)
        self.assertEqual(risk, "LOW")  # 90 ≥ 80
        tm_ids = [f["check_id"] for f in failed]
        self.assertIn("LZ_TM_001", tm_ids)
        self.assertIn("LZ_TM_002", tm_ids)

    def test_restore_script_missing(self):
        """
        No restore evidence: LZ_RST_001 (-15) fails.
        Score = 85, still LOW.
        """
        findings = _make_findings({"LZ_RST_001": False})
        score, risk, top5, failed = compute_score(findings)
        self.assertEqual(score, 85)
        self.assertEqual(risk, "LOW")
        self.assertEqual(failed[0]["check_id"], "LZ_RST_001")

    def test_secrets_policy_blocked(self):
        """
        LZ_SEC_001 (8pts) + LZ_SEC_002 (4pts) fail.
        Score = 88, still LOW. Confirms these checks contribute to score.
        """
        findings = _make_findings({
            "LZ_SEC_001": False,  # Redaction inactive (-8)
            "LZ_SEC_002": False,  # Secrets unclassified (-4)
        })
        score, risk, top5, failed = compute_score(findings)
        self.assertEqual(score, 88)
        self.assertEqual(risk, "LOW")

    def test_score_clamped_at_0(self):
        """All checks failing → score = 0, not negative."""
        findings = _make_findings({k: False for k in CHECK_WEIGHTS})
        score, risk, top5, failed = compute_score(findings)
        self.assertEqual(score, 0)
        self.assertGreaterEqual(score, 0)

    def test_score_clamped_at_100(self):
        """Score never exceeds 100."""
        findings = _make_findings()
        score, _, _, _ = compute_score(findings)
        self.assertLessEqual(score, 100)

    def test_deterministic_ordering(self):
        """
        Same findings → identical score + identical top5 remediation ordering.
        Two calls must produce byte-identical results.
        """
        findings = _make_findings({
            "LZ_TM_001": False,
            "LZ_TM_002": False,
            "LZ_SURF_001": False,
            "LZ_RST_001": False,
        })
        result1 = compute_score(findings)
        result2 = compute_score(findings)
        self.assertEqual(result1[0], result2[0], "Scores must match")
        self.assertEqual(result1[2], result2[2], "Top5 remediations must match")
        self.assertEqual(
            [f["check_id"] for f in result1[3]],
            [f["check_id"] for f in result2[3]],
            "Failed check ordering must be deterministic"
        )

    def test_remediation_ordering_by_weight(self):
        """
        Failed checks sorted by weight DESC, check_id ASC.
        LZ_SURF_001 (20pts) > LZ_RST_001 (15pts) > LZ_SURF_002 (10pts)
        """
        findings = _make_findings({
            "LZ_RST_001": False,   # 15 pts
            "LZ_SURF_001": False,  # 20 pts  ← should be first
            "LZ_SURF_002": False,  # 10 pts  ← should be third
        })
        _, _, _, failed = compute_score(findings)
        ids = [f["check_id"] for f in failed]
        self.assertEqual(ids[0], "LZ_SURF_001")  # weight 20
        self.assertEqual(ids[1], "LZ_RST_001")   # weight 15
        self.assertEqual(ids[2], "LZ_SURF_002")  # weight 10

    def test_missing_check_treated_conservatively(self):
        """
        A check in CHECK_WEIGHTS that's absent from findings
        contributes 0 to score (conservative / unknown = fail).
        """
        # Only include a subset of checks, all passing
        findings = [
            {"check_id": "LZ_SURF_001", "passed": True, "severity": "CRITICAL",
             "evidence": "ok", "remediation": "", "confidence": 1.0},
        ]
        score, _, _, failed = compute_score(findings)
        # Only LZ_SURF_001 (20pts) contributed; rest are missing → 0
        self.assertEqual(score, 20)
        missing_ids = [f["check_id"] for f in failed]
        self.assertNotIn("LZ_SURF_001", missing_ids)
        self.assertIn("LZ_TM_001", missing_ids)


if __name__ == "__main__":
    print("Running Lazarus score unit tests...")
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(__import__(__name__))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
