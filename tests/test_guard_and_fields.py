"""Shrink guard, response_fields_dropped, and sibling-file $refs. Run from the repo root:

    python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cx_api_spec as A  # noqa: E402
from test_placement import (cfg_for, make_raw, norm, op, read_text, scratch,  # noqa: E402
                            write_json, write_text)


def stoplight_source(paths, prefix="/api/x"):
    d = {"openapi": "3.0.0", "info": {"title": "S", "description": f"<b>{{Base_URL}}{prefix}</b>"},
         "paths": paths}
    s = A.Source("stoplight", "s1", "S", "s.yaml", "s.yaml", d)
    s.prefix, s.prefix_src, s.notes = A._stoplight_prefix(d)
    return s


def schema_op(props):
    return {"responses": {"200": {"description": "ok", "content": {"application/json": {
        "schema": {"type": "object", "properties": props}}}}}}


class ShrinkGuard(unittest.TestCase):
    def setUp(self):
        self.root = scratch()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.raw = make_raw(self.root)
        probe = norm(tempfile.mkdtemp(dir=self.root))
        self.assertEqual(A.run(cfg_for(self.root, self.raw, os.path.join(probe, "p"))), 0)
        spec = json.loads(read_text(os.path.join(probe, "p", "cxone_openapi.json")))
        self.ops = {(m.upper(), p) for p, item in spec["paths"].items() for m in item}

    def baseline(self, extra=()):
        paths = {}
        for m, p in sorted(self.ops):
            paths.setdefault(p, {})[m.lower()] = op("x")
        for p in extra:
            paths[p] = {"get": op("x")}
        path = os.path.join(self.root, "baseline.json")
        write_json(path, {"openapi": "3.0.3", "info": {"title": "b", "version": "1"},
                          "security": [{"B": []}],
                          "components": {"securitySchemes": {"B": {"type": "http", "scheme": "bearer"}}},
                          "paths": paths})
        return path

    def stage(self, baseline, out="g", used=None, allow=False):
        cfg = cfg_for(self.root, self.raw, out, baseline)
        cfg.allow_shrink = allow
        if used:
            cfg.used_endpoints = os.path.join(self.root, "used.txt")
            write_text(cfg.used_endpoints, used)
        code = A.run(cfg)
        out_dir = os.path.join(self.root, out)
        return (code, os.path.exists(os.path.join(out_dir, "cxone_openapi.json")),
                read_text(os.path.join(out_dir, "API-SPEC-REPORT.md")),
                os.path.exists(os.path.join(out_dir, "history.json")))

    def test_unchanged_baseline_does_not_fire(self):
        code, spec, report, _ = self.stage(self.baseline())
        self.assertEqual(code, 0)
        self.assertTrue(spec)
        self.assertNotIn("SHRINK GUARD", report)

    def test_more_than_five_percent_fewer_operations_fires(self):
        # 5 merged operations against a 6-operation baseline is 16.7% smaller
        code, spec, report, hist = self.stage(self.baseline(extra=["/api/vanished"]))
        self.assertEqual(code, A.EXIT_SHRINK)
        self.assertFalse(spec)                  # the spec is not written
        self.assertFalse(hist)                  # and a blocked run must not move history
        self.assertIn("SHRINK GUARD FIRED", report)          # but the report is, and says why
        self.assertIn("fewer than the baseline", report)

    def test_threshold_is_strictly_more_than_five_percent(self):
        def fake(n):
            return {"merged": {("GET", f"/{i}"): {} for i in range(n)}}
        self.assertEqual(A.shrink_check(fake(95), 100, []), [])
        self.assertEqual(len(A.shrink_check(fake(94), 100, [])), 1)
        self.assertEqual(A.shrink_check(fake(1), None, []), [])       # no baseline, no guard

    def test_used_endpoint_missing_from_merge_fires(self):
        code, spec, report, _ = self.stage(self.baseline(), used="GET /api/not-there\n")
        self.assertEqual(code, A.EXIT_SHRINK)
        self.assertFalse(spec)
        self.assertIn("--used-endpoints", report)
        self.assertIn("/api/not-there", report)

    def test_used_endpoint_present_does_not_fire(self):
        code, spec, _, _ = self.stage(self.baseline(), used="GET /api/contributors/csv\n")
        self.assertEqual((code, spec), (0, True))

    def test_allow_shrink_writes_the_spec_and_still_reports(self):
        code, spec, report, _ = self.stage(self.baseline(extra=["/api/vanished"]), allow=True)
        self.assertNotEqual(code, A.EXIT_SHRINK)
        self.assertTrue(spec)
        self.assertIn("SHRINK GUARD FIRED (overridden by --allow-shrink)", report)

    def test_flag_exists(self):
        import argparse
        ap = argparse.ArgumentParser()
        A.add_arguments(ap)
        self.assertFalse(A.build_cfg(ap.parse_args([])).allow_shrink)
        self.assertTrue(A.build_cfg(ap.parse_args(["--allow-shrink"])).allow_shrink)


class ResponseFieldsDropped(unittest.TestCase):
    def live(self):
        return A.Source("live", "L", "L", "l.yaml", "l.yaml",
                        {"paths": {"/p/{id}": {"get": schema_op({"id": {"type": "string"}})}}}, "/api/x", "x")

    def baseline_with(self, props):
        return {"openapi": "3.0.3", "info": {"title": "b", "version": "1"},
                "paths": {"/api/x/p/{id}": {"get": schema_op(props)}}}

    def drift(self, baseline=None, sl=None):
        lives, sls = [self.live()], ([sl] if sl else [])
        info, sl_pref = A.learn_prefixes(lives, sls)
        res = A.build_spec(lives, [], sls, [], {}, info, sl_pref, "2026-01-01")
        hist = A.update_history({}, res["live_sigs"], "2026-01-01")
        return A.compute_drift(res, baseline, hist, []), res

    def test_unchanged_baseline_reports_nothing(self):
        _, res = self.drift()
        d, _ = self.drift(baseline=res["doc"])
        self.assertEqual(d["response_fields_dropped"], [])

    def test_fields_in_the_baseline_but_not_in_the_merge(self):
        props = {"id": {"type": "string"}, "repoId": {"type": "string"}, "privatePackage": {"type": "boolean"}}
        d, _ = self.drift(baseline=self.baseline_with(props))
        (row,) = d["response_fields_dropped"]
        self.assertEqual(row["key"], "GET /api/x/p/{id}")
        self.assertEqual(row["properties"], ["privatePackage", "repoId"])
        self.assertEqual(row["in_baseline"], ["privatePackage", "repoId"])
        self.assertEqual(d["summary"]["response_fields_dropped"], 1)

    def test_fields_in_stoplight_but_not_in_the_live_schema(self):
        sl = stoplight_source({"/p/{id}": {"get": schema_op(
            {"id": {"type": "string"}, "imported_proj_name": {"type": "string"}})}})
        d, _ = self.drift(sl=sl)
        (row,) = d["response_fields_dropped"]
        self.assertEqual((row["in_stoplight"], row["in_baseline"]), (["imported_proj_name"], []))

    def test_nested_properties_are_found(self):
        props = {"id": {"type": "string"}, "tags": {"type": "object", "properties": {"test": {"type": "string"}}}}
        d, _ = self.drift(baseline=self.baseline_with(props))
        self.assertEqual(d["response_fields_dropped"][0]["properties"], ["tags", "tags.test"])

    def test_informational_only(self):
        d, res = self.drift(baseline=self.baseline_with({"id": {"type": "string"}, "repoId": {"type": "string"}}))
        self.assertEqual(d["used_endpoints"]["breaking"], [])
        self.assertNotIn("repoId", json.dumps(res["doc"]))      # the spec is not altered


class SiblingRefs(unittest.TestCase):
    def build(self, ref, refs=()):
        s = stoplight_source({"/a": {"get": {"parameters": [{"$ref": ref}],
                                             "responses": {"200": {"description": "ok"}}}}})
        info, sl_pref = A.learn_prefixes([], [s])
        return A.build_spec([], [], [s], [], {}, info, sl_pref, "2026-01-01", refs=list(refs))

    def test_cross_file_ref_resolves_against_a_fetched_sibling(self):
        sib = A.Source("stoplight", "ref:sibling.yaml", "sibling.yaml", "sibling.yaml", "stoplight/refs/sibling.yaml",
                       {"components": {"parameters": {"authHeader": {
                           "name": "X-Auth", "in": "header", "schema": {"type": "string"}}}}})
        res = self.build("./sibling.yaml#/components/parameters/authHeader", [sib])
        self.assertEqual(res["unresolved"], [])
        self.assertEqual(A.dangling_refs(res["doc"]), [])
        self.assertIn("authHeader", res["doc"]["components"]["parameters"])
        self.assertEqual(res["doc"]["paths"]["/api/x/a"]["get"]["x-source"], "stoplight")

    def test_a_sibling_that_does_not_exist_stays_unresolved(self):
        res = self.build("./missing.yaml#/components/parameters/p")
        self.assertEqual([u["ref"] for u in res["unresolved"]], ["./missing.yaml#/components/parameters/p"])

    def test_sibling_files_contribute_no_operations(self):
        sib = A.Source("stoplight", "ref:sibling.yaml", "sibling.yaml", "sibling.yaml", "stoplight/refs/sibling.yaml",
                       {"paths": {"/only-in-sibling": {"get": op("x")}}, "components": {}})
        res = self.build("./sibling.yaml#/components/parameters/p", [sib])
        self.assertNotIn("/api/x/only-in-sibling", res["doc"]["paths"])

    def test_ref_regex_finds_yaml_refs_only(self):
        text = "a:\n  $ref: './sastResults_copy.yaml#/components/x'\n  b: [Scans.yaml/paths/~1x](link)\n" \
               "  $ref: \"Scans.yaml#/c\""
        self.assertEqual([m.group(1) for m in A._REF_FILE.finditer(text)], ["sastResults_copy.yaml", "Scans.yaml"])


if __name__ == "__main__":
    unittest.main()
