"""--fresh: start clean without deleting anything by hand.

    python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import stat
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cx_api_spec as A  # noqa: E402
import cx_docs_mirror as D  # noqa: E402
from test_output_layout import docs_args  # noqa: E402
from test_placement import cfg_for, make_raw, norm, read_text, scratch, write_json, write_text  # noqa: E402


def make_file(root: str, rel: str, text: str = "x") -> str:
    path = norm(os.path.join(root, *rel.split("/")))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_text(path, text)
    return path


class Config(unittest.TestCase):
    def test_docs_fresh_forces_a_full_download(self):
        self.assertFalse(D.build_cfg(docs_args()).fresh)
        cfg = D.build_cfg(docs_args("--fresh"))
        self.assertTrue(cfg.fresh and cfg.force)

    def test_flag_is_defined_once_for_both_jobs(self):
        import argparse
        ap = argparse.ArgumentParser()
        A.add_arguments(ap)
        self.assertTrue(A.build_cfg(ap.parse_args(["--fresh"])).fresh)
        self.assertFalse(A.build_cfg(ap.parse_args([])).fresh)


class FailFast(unittest.TestCase):
    def test_missing_baseline_or_used_file_stops_before_any_download(self):
        root = scratch()
        self.addCleanup(shutil.rmtree, root, True)
        cfg = cfg_for(root, "", "out")
        cfg.baseline = os.path.join(root, "no-such-baseline.json")
        cfg.from_raw = None
        called = []

        async def fake_fetch(c, prev):
            called.append(1)
            raise AssertionError("must not fetch")

        original, A.fetch_all = A.fetch_all, fake_fetch
        try:
            self.assertEqual(A.run(cfg), A.EXIT_FATAL)
            cfg.baseline, cfg.used_endpoints = None, os.path.join(root, "no-such-used.txt")
            self.assertEqual(A.run(cfg), A.EXIT_FATAL)
        finally:
            A.fetch_all = original
        self.assertEqual(called, [])
        with self.assertRaises(A.FatalError):
            A.check_inputs(cfg)


class Removal(unittest.TestCase):
    def setUp(self):
        self.root = scratch()
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_read_only_files_and_trees_are_removed(self):
        stuck = make_file(self.root, "raw/live/a.yaml")
        os.chmod(stuck, stat.S_IREAD)                       # what OneDrive and Windows love to do
        self.assertTrue(A.force_remove(os.path.join(self.root, "raw")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "raw")))

    def test_missing_path_is_fine(self):
        self.assertTrue(A.force_remove(os.path.join(self.root, "nope")))

    def test_prune_keeps_only_this_runs_files(self):
        for rel in ("live/new.yaml", "live/old.yaml", "stoplight/refs/gone.yaml", "provenance.json"):
            make_file(self.root, "raw/" + rel)
        raw = norm(os.path.join(self.root, "raw"))
        self.assertEqual(A.prune_raw(Path(raw), {"live/new.yaml"}), 0)
        left = sorted(os.path.relpath(os.path.join(d, f), raw).replace(os.sep, "/")
                      for d, _, files in os.walk(raw) for f in files)
        self.assertEqual(left, ["live/new.yaml", "provenance.json"])
        self.assertFalse(os.path.exists(os.path.join(raw, "stoplight")))      # emptied directories go too


class FreshApiRun(unittest.TestCase):
    def setUp(self):
        self.root = scratch()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.fetched_raw = make_raw(self.root)
        self.out = os.path.join(self.root, "out")
        make_file(self.out, "raw/live/old.yaml", "stale: true")      # what a previous run left behind
        old = {"live_sig": "x", "live_verified": "2020-01-01", "first_seen": "2020-01-01",
               "last_seen": "2020-01-01", "live_seen": True, "missing_runs": 0}
        write_json(os.path.join(self.out, "history.json"), {
            "version": A.HISTORY_VERSION, "runs": ["2020-01-01"],
            "ops": {"GET /api/contributors/csv": dict(old), "GET /api/ghost": dict(old, missing_runs=5)}})
        self.seen_prev = []

    def run_stage(self, fresh):
        async def fake_fetch(cfg, prev):
            self.seen_prev.append(prev)
            return A.RawSet.load(Path(self.fetched_raw)), []

        cfg = cfg_for(self.root, "", "out")
        cfg.from_raw, cfg.fresh = None, fresh
        original, A.fetch_all = A.fetch_all, fake_fetch
        try:
            self.assertEqual(A.run(cfg), 0)
        finally:
            A.fetch_all = original
        return json.loads(read_text(os.path.join(self.out, "api-spec-drift.json")))

    def test_fresh_ignores_history_and_cached_downloads(self):
        drift = self.run_stage(fresh=True)
        self.assertIsNone(self.seen_prev[0])                                    # no stale fallback
        self.assertFalse(os.path.exists(os.path.join(self.out, "raw", "live", "old.yaml")))
        self.assertEqual(drift["removed_candidate"], [])                        # 5 missing runs forgotten
        hist = json.loads(read_text(os.path.join(self.out, "history.json")))
        self.assertEqual(hist["runs"], ["2026-01-01"])
        self.assertNotIn("GET /api/ghost", hist["ops"])

    def test_without_fresh_the_history_is_used(self):
        drift = self.run_stage(fresh=False)
        self.assertIsNotNone(self.seen_prev[0])
        self.assertEqual([r["key"] for r in drift["removed_candidate"]], ["GET /api/ghost"])


if __name__ == "__main__":
    unittest.main()
