#!/usr/bin/env python3
"""
Unit tests for gtm_funnel_export.py
TASK_ID: A-FUN-P4-001

Tests:
  1.  Missing sources → status=NONE, no crash
  2.  Valid sources → status=READY
  3.  Creates gtm_exports/ directory if missing
  4.  funnel_export.json matches source content byte-for-byte
  5.  funnel_export.md matches source content byte-for-byte
  6.  export_manifest.json contains all required fields
  7.  manifest generated_ts comes from source JSON (not datetime.now)
  8.  manifest system_id matches provided hostname
  9.  Determinism: same sources → identical manifest hashes twice
  10. Determinism: json_hash + md_hash identical across two runs
  11. emit GTM_EXPORT_READY on success
  12. GTM_EXPORT_READY event contains attach_rate, expansion_rate, hashes
  13. read_export_status returns READY when manifest present
  14. read_export_status returns NONE when manifest missing
  15. Exits 0 (no exception) on any input combination
  16. manifest key ordering follows spec

Usage:
  python3 test_gtm_funnel_export.py
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

from gtm_funnel_export import (  # noqa: E402
    run_export,
    read_export_status,
    EVT_EXPORT_READY,
    _MANIFEST_KEY_ORDER,
    _sha256_short,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_JSON = json.dumps({
    "window_days": 7,
    "radcheck_runs_7d": 0,
    "sentinel_recommended_7d": 3,
    "sentinel_enabled_present": True,
    "agent911_views_7d": 10,
    "agent911_expansions_7d": 1,
    "sentinel_attach_rate": 0.0,
    "agent911_expansion_rate": 0.100,
    "ts": "2026-02-28T00:00:00Z",
}, indent=2) + "\n"

SAMPLE_MD = (
    "# ACME Funnel Report\n\n"
    "## 1. Executive Summary\n"
    "| RadCheck runs (7d) | 0 |\n"
)


def _setup_tmp_sources(tmpdir: str, json_content: str = SAMPLE_JSON,
                        md_content: str = SAMPLE_MD) -> tuple:
    json_path = os.path.join(tmpdir, "gtm_funnel_weekly.json")
    md_path   = os.path.join(tmpdir, "gtm_funnel_weekly.md")
    with open(json_path, "w") as f: f.write(json_content)
    with open(md_path,   "w") as f: f.write(md_content)
    return json_path, md_path


class FunnelExportTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.ops    = os.path.join(self.tmpdir, "ops.log")
        self.outdir = os.path.join(self.tmpdir, "exports")

    def _run(self, json_content=SAMPLE_JSON, md_content=SAMPLE_MD,
             hostname="testhost") -> dict:
        json_path, md_path = _setup_tmp_sources(
            self.tmpdir, json_content, md_content)
        return run_export(
            json_src=json_path,
            md_src=md_path,
            out_dir=self.outdir,
            ops_path=self.ops,
            hostname=hostname,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMissingInputs(FunnelExportTestCase):
    """Test 1 — missing sources → NONE, no crash."""

    def test_missing_sources_returns_none(self):
        r = run_export(
            json_src="/nonexistent/a.json",
            md_src="/nonexistent/b.md",
            out_dir=self.outdir,
            ops_path=self.ops,
        )
        self.assertEqual(r["status"], "NONE")

    def test_no_exception_on_missing(self):
        try:
            run_export(
                json_src="/nonexistent/a.json",
                md_src="/nonexistent/b.md",
                out_dir=self.outdir,
                ops_path=self.ops,
            )
        except Exception as e:
            self.fail(f"run_export raised {e}")


class TestValidExport(FunnelExportTestCase):
    """Tests 2–3 — valid sources produce READY status and directory."""

    def test_status_ready(self):
        r = self._run()
        self.assertEqual(r["status"], "READY")

    def test_creates_output_directory(self):
        self._run()
        self.assertTrue(os.path.isdir(self.outdir))


class TestFileContents(FunnelExportTestCase):
    """Tests 4–5 — output files match source byte-for-byte."""

    def test_json_copy_matches_source(self):
        self._run()
        with open(os.path.join(self.outdir, "funnel_export.json")) as f:
            exported = f.read()
        self.assertEqual(exported, SAMPLE_JSON)

    def test_md_copy_matches_source(self):
        self._run()
        with open(os.path.join(self.outdir, "funnel_export.md")) as f:
            exported = f.read()
        self.assertEqual(exported, SAMPLE_MD)


class TestManifest(FunnelExportTestCase):
    """Tests 6–8, 16 — export_manifest.json content and ordering."""

    def _manifest(self) -> dict:
        self._run()
        with open(os.path.join(self.outdir, "export_manifest.json")) as f:
            return json.load(f)

    def test_manifest_has_all_required_fields(self):
        m = self._manifest()
        for field in _MANIFEST_KEY_ORDER:
            self.assertIn(field, m, f"Missing manifest field: {field}")

    def test_generated_ts_from_source_json(self):
        m = self._manifest()
        # Must match the ts field from SAMPLE_JSON
        self.assertEqual(m["generated_ts"], "2026-02-28T00:00:00Z")

    def test_system_id_matches_hostname(self):
        self._run(hostname="my-gtm-host")
        with open(os.path.join(self.outdir, "export_manifest.json")) as f:
            m = json.load(f)
        self.assertEqual(m["system_id"], "my-gtm-host")

    def test_manifest_key_ordering(self):
        m = self._manifest()
        actual_keys = list(m.keys())
        spec_positions = [actual_keys.index(k) for k in _MANIFEST_KEY_ORDER if k in actual_keys]
        self.assertEqual(spec_positions, sorted(spec_positions))


class TestDeterminism(FunnelExportTestCase):
    """Tests 9–10 — same sources → identical hashes across two runs."""

    def test_json_hash_identical(self):
        r1 = self._run()
        r2 = self._run()
        self.assertEqual(r1["json_hash"], r2["json_hash"])
        self.assertNotEqual(r1["json_hash"], "")

    def test_md_hash_identical(self):
        r1 = self._run()
        r2 = self._run()
        self.assertEqual(r1["md_hash"], r2["md_hash"])

    def test_manifest_deterministic(self):
        self._run()
        with open(os.path.join(self.outdir, "export_manifest.json")) as f:
            m1 = f.read()
        self._run()
        with open(os.path.join(self.outdir, "export_manifest.json")) as f:
            m2 = f.read()
        self.assertEqual(
            hashlib.sha256(m1.encode()).hexdigest(),
            hashlib.sha256(m2.encode()).hexdigest(),
            "Manifest content differs between two runs with same source",
        )


class TestNDJSONEvent(FunnelExportTestCase):
    """Tests 11–12 — GTM_EXPORT_READY event."""

    def test_emits_event_on_success(self):
        r = self._run()
        self.assertTrue(r["event_emitted"])
        with open(self.ops) as f:
            rec = json.loads(f.read().strip())
        self.assertEqual(rec["event"], EVT_EXPORT_READY)

    def test_event_has_required_fields(self):
        self._run()
        with open(self.ops) as f:
            rec = json.loads(f.read().strip())
        for field in ("attach_rate", "expansion_rate", "json_hash",
                      "md_hash", "export_status"):
            self.assertIn(field, rec, f"Missing event field: {field}")


class TestReadExportStatus(FunnelExportTestCase):
    """Tests 13–14 — read_export_status."""

    def test_ready_when_manifest_present(self):
        self._run()
        status = read_export_status(self.outdir)
        self.assertEqual(status, "READY")

    def test_none_when_manifest_missing(self):
        status = read_export_status(os.path.join(self.tmpdir, "nonexistent_dir"))
        self.assertEqual(status, "NONE")


class TestSafetyAndEdgeCases(FunnelExportTestCase):
    """Test 15 — exits 0 on any input."""

    def test_only_json_present(self):
        json_path = os.path.join(self.tmpdir, "gtm.json")
        with open(json_path, "w") as f:
            f.write(SAMPLE_JSON)
        try:
            r = run_export(
                json_src=json_path,
                md_src="/nonexistent/b.md",
                out_dir=self.outdir,
                ops_path=self.ops,
            )
            # Should succeed (md content will be empty string, but json is present)
            self.assertIn(r["status"], ("READY", "PARTIAL", "NONE"))
        except Exception as e:
            self.fail(f"run_export raised: {e}")

    def test_sha256_short_length(self):
        h = _sha256_short("test content")
        self.assertEqual(len(h), 16)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite  = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
