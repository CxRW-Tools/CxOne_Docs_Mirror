"""Output layout and temp-file cleanup. Run from the repo root:

    python -m unittest discover -s tests -v
"""

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cx_api_spec as A  # noqa: E402
import cx_docs_mirror as D  # noqa: E402


def docs_args(*extra):
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all")
    for flag in ("--url", "--md-path", "--md-out", "--json-path", "--html-dir", "--doc-title"):
        ap.add_argument(flag)
    for flag in ("--include", "--exclude", "--title-exclude"):
        ap.add_argument(flag, nargs="*")
    for flag in ("--min-chars", "--concurrency", "--depth", "--timeout", "--retries", "--max-pages", "--rescue-depth"):
        ap.add_argument(flag, type=int)
    ap.add_argument("--wait", type=float)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--keep-temp", action="store_true")
    return ap.parse_args(list(extra))


def scratch_dir() -> str:
    """A fresh, normalised temp directory path (the caller removes it)."""
    return os.path.abspath(os.path.normpath(tempfile.mkdtemp()))


class Layout(unittest.TestCase):
    def test_docs_defaults_live_under_docs(self):
        cfg = D.build_cfg(docs_args())
        for p in (cfg.html_dir, cfg.json_path, cfg.md_path):
            self.assertEqual(Path(p).parts[0], "docs", p)
        # state, manifest, change report and failures.log sit next to the pages dir
        self.assertEqual(Path(cfg.html_dir).parent, Path("docs"))

    def test_api_defaults_live_under_api(self):
        ap = argparse.ArgumentParser()
        A.add_arguments(ap)
        cfg = A.build_cfg(ap.parse_args([]))
        self.assertEqual(Path(cfg.out_dir), Path("api"))
        self.assertEqual(Path(A.API_OVERLAY_DIR).parts[0], "api")


class AtomicWrites(unittest.TestCase):
    def setUp(self):
        self.root = scratch_dir()
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        self.dir = Path(self.root)

    def leftovers(self):
        return [p.name for p in self.dir.rglob("*.tmp")]

    def test_write_leaves_no_tmp_file(self):
        for write in (D._write_text, A.write_atomic):
            target = self.dir / "sub" / "out.txt"
            write(target, "hello")
            self.assertEqual(target.read_text(encoding="utf-8"), "hello")
            self.assertEqual(self.leftovers(), [])

    def test_failed_write_keeps_old_file_and_removes_tmp(self):
        for write in (D._write_text, A.write_atomic):
            target = self.dir / "out.txt"
            target.write_text("old", encoding="utf-8")
            with mock.patch("os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write(target, "new")
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertEqual(self.leftovers(), [])

    def test_sweep_removes_stale_tmp_files(self):
        (self.dir / "pages" / "en").mkdir(parents=True)
        stale = [self.dir / "a.json.tmp", self.dir / "pages" / "en" / "p.html.tmp"]
        keep = self.dir / "pages" / "en" / "p.html"
        for f in stale + [keep]:
            f.write_text("x", encoding="utf-8")
        D._sweep_tmp(self.dir)
        A.sweep_tmp(self.dir)
        self.assertEqual(self.leftovers(), [])
        self.assertTrue(keep.exists())

    def test_raw_set_save_is_atomic(self):
        rs = A.RawSet()
        rs.put("live/x.yaml", b"a: 1", "https://example.test/x")
        rs.save(self.dir / "raw")
        self.assertEqual((self.dir / "raw" / "live" / "x.yaml").read_bytes(), b"a: 1")
        self.assertEqual(self.leftovers(), [])


class TempCleanup(unittest.TestCase):
    RECORD = {"status": "keep", "markdown": "# T\n\nbody", "product": "P", "breadcrumb": ["P"],
              "title": "T", "modified": "", "file": "en/t.html"}

    def run_combine(self, keep_temp):
        root = scratch_dir()
        self.addCleanup(__import__("shutil").rmtree, root, True)
        out_dir = os.path.join(root, "docs")
        os.makedirs(out_dir)
        extract = os.path.join(out_dir, "cx-extracted.json")
        with open(extract, "w", encoding="utf-8") as fh:
            json.dump([self.RECORD], fh)
        cfg = D.build_cfg(docs_args("--json-path", extract, "--md-path", os.path.join(out_dir, "out.md"),
                                    *(["--keep-temp"] if keep_temp else [])))
        D.stage_combine(cfg)
        return (os.path.exists(cfg.md_path), os.path.exists(extract),
                [f for f in os.listdir(out_dir) if f.endswith(".tmp")])

    def test_extract_json_is_removed_after_combine(self):
        md, extract, tmp = self.run_combine(keep_temp=False)
        self.assertTrue(md)
        self.assertFalse(extract)
        self.assertEqual(tmp, [])

    def test_keep_temp_flag_keeps_it(self):
        md, extract, _ = self.run_combine(keep_temp=True)
        self.assertTrue(md and extract)

    def test_combine_without_extract_is_a_clear_error(self):
        cfg = D.build_cfg(docs_args("--json-path", "no/such/extract.json"))
        with self.assertRaises(SystemExit) as cm:
            D.stage_combine(cfg)
        self.assertIn("extract stage first", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
