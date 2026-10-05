"""Offline tests for cx_api_spec.py. Run from the repo root:

    python -m unittest discover -s tests -v
"""

import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cx_api_spec as A  # noqa: E402

DATE = "2026-01-01"


def live_doc(extra_paths=None, enum=("Queued", "Running")):
    paths = {
        "/": {"get": {
            "operationId": "getScans", "summary": "live summary",
            "parameters": [{"name": "status", "in": "query", "schema": {"type": "string", "enum": list(enum)}}],
            "responses": {200: {"description": "ok", "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/Scan"}}}}}}},
        "/{id}": {"get": {"responses": {"200": {"description": "ok"}}}},
        "/items": {"get": {"responses": {"200": {"description": "ok"}}}},
        "/export": {"get": {"responses": {"200": {"description": "ok"}}}},
    }
    paths.update(extra_paths or {})
    return A._strkeys({
        "openapi": "3.0.2", "servers": [{"url": "/api/scans"}], "security": [{"Login": []}],
        "components": {
            "securitySchemes": {"Login": {"type": "oauth2", "flows": {}}},
            "schemas": {"Scan": {"type": "object", "required": ["id"],
                                 "properties": {"id": {"type": "string"}, "state": {"type": "string"}}}}},
        "paths": paths})


def sl_doc():
    return {
        "openapi": "3.0.0",
        "info": {"title": "Scans", "description": "<details><summary><b>{Base_URL}/api/scans</b></summary></details>"},
        "paths": {
            "/": {"get": {"operationId": "x", "summary": "Retrieve scans", "description": "Stoplight prose",
                          "x-stoplight": {"id": "abc"}, "responses": {"200": {"description": "ok"}}}},
            "/{id}": {"get": {"summary": "One scan", "responses": {"200": {"description": "ok"}}}},
            "/items": {"get": {"summary": "Items", "responses": {"200": {"description": "ok"}}}},
            "/export": {"get": {"summary": "Export", "responses": {"200": {"description": "ok"}}}},
            "/legacy": {"get": {"summary": "Only in Stoplight", "responses": {"200": {"description": "ok"}}}},
        }}


def src(origin, key, doc, name=None):
    s = A.Source(origin, key, name or key, f"{key}.yaml", f"{key}.yaml", doc)
    if origin == "live":
        servers = doc.get("servers") or []
        s.prefix = A.server_prefix(servers[0]["url"]) if servers else None
    elif origin == "stoplight":
        s.prefix, s.prefix_src, s.notes = A._stoplight_prefix(doc)
    return s


def build(lives, sls, overlays=(), history=None):
    info, sl_pref = A.learn_prefixes(lives, sls)
    return A.build_spec(lives, [], sls, list(overlays), history or {}, info, sl_pref, DATE)


def temp_file(text: str, suffix: str) -> str:
    fd, name = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return name


class Helpers(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(A.norm_path("/a/{id}/b/"), "/a/{}/b")
        self.assertEqual(A.norm_path("/"), "/")
        self.assertEqual(A.join_path("/api/x", "/"), "/api/x")
        self.assertEqual(A.join_path("/api/x/", "/y"), "/api/x/y")
        self.assertEqual(A.join_path("", "/y"), "/y")

    def test_server_prefix(self):
        self.assertEqual(A.server_prefix("/api/x/"), "/api/x")
        self.assertEqual(A.server_prefix("https://ast.checkmarx.net/api/x"), "/api/x")
        self.assertIsNone(A.server_prefix("REPOS"))        # real value in the live catalog
        self.assertIsNone(A.server_prefix("/api/{tenant}"))
        self.assertIsNone(A.server_prefix(None))

    def test_stoplight_prefix_prefers_description_over_bad_servers(self):
        doc = sl_doc()
        doc["servers"] = [{"url": "https://eu-2.ast.checkmarx.net/scans"}]
        self.assertEqual(A._stoplight_prefix(doc)[0], "/api/scans")

    def test_checked_url_blocks_other_hosts(self):
        ok = {"ast.checkmarx.net"}
        self.assertTrue(A.checked_url("https://ast.checkmarx.net/x", ok))
        for bad in ("http://ast.checkmarx.net/x", "https://evil.example/x",
                    "https://ast.checkmarx.net@evil.example/x", "https://ast.checkmarx.net/a b"):
            with self.assertRaises(ValueError):
                A.checked_url(bad, ok)

    def test_raw_save_refuses_traversal(self):
        rs = A.RawSet()
        rs.blobs["../escape.txt"] = b"x"
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(A.FatalError):
                rs.save(Path(d) / "raw")


class Prefixes(unittest.TestCase):
    def test_agreeing_sources_are_matched(self):
        info, _ = A.learn_prefixes([src("live", "SCANS", live_doc())], [src("stoplight", "s1", sl_doc())])
        self.assertEqual(info["SCANS"]["prefix"], "/api/scans")
        self.assertEqual(info["SCANS"]["confidence"], "matched")

    def test_declared_only(self):
        info, _ = A.learn_prefixes([src("live", "SCANS", live_doc())], [])
        self.assertEqual(info["SCANS"]["confidence"], "declared")

    def test_baseline_is_not_an_input(self):
        import inspect
        self.assertEqual(list(inspect.signature(A.learn_prefixes).parameters), ["lives", "sls"])

    def test_disagreement_is_flagged_not_guessed(self):
        doc = live_doc()
        doc["servers"] = [{"url": "/api/other"}]
        info, _ = A.learn_prefixes([src("live", "SCANS", doc)], [src("stoplight", "s1", sl_doc())])
        self.assertEqual(info["SCANS"]["confidence"], "conflict")
        self.assertEqual(info["SCANS"]["prefix"], "/api/other")    # the live prefix wins, flagged for a human
        self.assertEqual(info["SCANS"]["stoplight"], "/api/scans")

    def test_unusable_server_means_unknown(self):
        doc = live_doc()
        doc["servers"] = [{"url": "REPOS"}]
        info, _ = A.learn_prefixes([src("live", "X", doc)], [])
        self.assertEqual(info["X"]["confidence"], "unknown")


class Merge(unittest.TestCase):
    def setUp(self):
        self.res = build([src("live", "SCANS", live_doc(), "Scans")], [src("stoplight", "s1", sl_doc(), "Scans API")])
        self.doc = self.res["doc"]

    def test_live_schema_wins_stoplight_prose_wins(self):
        op = self.doc["paths"]["/api/scans"]["get"]
        self.assertEqual(op["x-source"], "both")
        self.assertEqual(op["summary"], "Retrieve scans")
        self.assertEqual(op["description"], "Stoplight prose")
        self.assertEqual(op["parameters"][0]["schema"]["enum"], ["Queued", "Running"])
        self.assertEqual(op["operationId"], "getScans")
        self.assertEqual(op["x-stoplight-id"], "abc")
        self.assertEqual(op["x-service"], "Scans")
        self.assertEqual(op["x-gateway-prefix"], "/api/scans")
        self.assertEqual(op["x-live-verified"], DATE)

    def test_stoplight_only_is_kept_and_flagged(self):
        op = self.doc["paths"]["/api/scans/legacy"]["get"]
        self.assertEqual(op["x-source"], "stoplight")
        self.assertTrue(op["x-live-missing"])
        self.assertNotIn("x-live-verified", op)

    def test_valid_shape(self):
        self.assertEqual(self.doc["openapi"], "3.0.3")
        self.assertEqual(A.dangling_refs(self.doc), [])
        self.assertEqual(self.doc["info"]["x-last-synced"], DATE)
        self.assertEqual(self.doc["security"], [{"BearerAuth": []}])
        self.assertNotIn("security", self.doc["paths"]["/api/scans"]["get"])   # inherits the global default
        self.assertEqual(list(self.doc["components"]["securitySchemes"]), ["BearerAuth"])

    def test_integer_status_codes_become_strings(self):
        self.assertIn("200", self.doc["paths"]["/api/scans"]["get"]["responses"])

    def test_undeclared_path_param_is_synthesized(self):
        p = self.doc["paths"]["/api/scans/{id}"]["get"]["parameters"][0]
        self.assertEqual((p["name"], p["in"], p["required"], p["x-synthesized"]), ("id", "path", True, True))

    def test_same_name_different_schema_does_not_collide(self):
        other = A._strkeys({
            "openapi": "3.0.0", "servers": [{"url": "/api/other"}],
            "components": {"schemas": {"Scan": {"type": "string"}}},
            "paths": {"/": {"get": {"responses": {"200": {"description": "ok", "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/Scan"}}}}}}}}})
        res = build([src("live", "A_SCANS", live_doc(), "Scans"), src("live", "B_OTHER", other, "Other")], [])
        schemas = res["doc"]["components"]["schemas"]
        self.assertEqual(schemas["Scan"]["type"], "object")
        self.assertEqual(schemas["Scan_B_OTHER"], {"type": "string"})
        ref = res["doc"]["paths"]["/api/other"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(ref["$ref"], "#/components/schemas/Scan_B_OTHER")

    def test_identical_component_is_shared(self):
        twin = live_doc()
        twin["servers"] = [{"url": "/api/twin"}]
        res = build([src("live", "SCANS", live_doc()), src("live", "TWIN", twin)], [])
        self.assertEqual([k for k in res["doc"]["components"]["schemas"] if k.startswith("Scan")], ["Scan"])

    def test_openapi_31_is_downgraded(self):
        d = A._strkeys({
            "openapi": "3.1.0", "servers": [{"url": "/api/n"}],
            "paths": {"/": {"get": {"responses": {"200": {"description": "ok", "content": {"application/json": {
                "schema": {"type": ["string", "null"], "examples": ["a"], "const": "a",
                           "exclusiveMinimum": 3, "$schema": "x"}}}}}}}}})
        res = build([src("live", "N", d)], [])
        s = res["doc"]["paths"]["/api/n"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(s, {"type": "string", "nullable": True, "example": "a", "enum": ["a"],
                             "minimum": 3, "exclusiveMinimum": True})

    def test_unresolvable_ref_is_logged_not_dangling(self):
        d = live_doc()
        d["paths"]["/"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] = {
            "$ref": "./missing.yaml#/components/schemas/X"}
        res = build([src("live", "SCANS", d)], [])
        self.assertEqual(res["unresolved"][0]["ref"], "./missing.yaml#/components/schemas/X")
        self.assertEqual(A.dangling_refs(res["doc"]), [])

    def test_output_is_deterministic(self):
        a = build([src("live", "SCANS", live_doc(), "Scans")], [src("stoplight", "s1", sl_doc(), "Scans API")])
        b = build([src("live", "SCANS", live_doc(), "Scans")], [src("stoplight", "s1", sl_doc(), "Scans API")])
        self.assertEqual(A.canon(a["doc"]), A.canon(b["doc"]))

    def test_unplaced_live_operations_are_reported_not_guessed(self):
        d = live_doc()
        d["servers"] = [{"url": "REPOS"}]
        res = build([src("live", "BAD", d)], [])
        self.assertEqual(res["doc"]["paths"], {})
        self.assertEqual(len(res["live_unplaced"]), 4)


class Overlay(unittest.TestCase):
    def overlay(self):
        return src("overlay", "mine", A._strkeys({
            "paths": {"/api/credits/info": {"get": {"x-source": "hand added", "operationId": "credits",
                                                    "responses": {"200": {"description": "ok"}}}},
                      "/api/scans": {"get": {"responses": {"200": {"description": "pinned"}}}}}}))

    def test_overlay_is_applied_and_flags_covered_entries(self):
        res = build([src("live", "SCANS", live_doc())], [], [self.overlay()])
        op = res["doc"]["paths"]["/api/credits/info"]["get"]
        self.assertEqual((op["x-source"], op["x-overlay-source"]), ("overlay", "hand added"))
        self.assertEqual(res["doc"]["paths"]["/api/scans"]["get"]["responses"]["200"]["description"], "pinned")
        self.assertEqual([c["key"] for c in res["overlay_covered"]], ["GET /api/scans"])
        self.assertEqual(len(res["overlay_applied"]), 2)


class Drift(unittest.TestCase):
    def drift(self, baseline, **kw):
        res = build([src("live", "SCANS", live_doc(**kw))], [])
        return A.compute_drift(res, baseline, A.update_history({}, res["live_sigs"], DATE), [])

    def baseline(self, enum=("Queued", "Running"), extra=True):
        res = build([src("live", "SCANS", live_doc(enum=enum))], [])
        doc = copy.deepcopy(res["doc"])
        if extra:
            doc["paths"]["/api/gone"] = {"get": {"responses": {"200": {"description": "ok"}}}}
        return doc

    def test_no_change(self):
        d = self.drift(self.baseline(extra=False))
        self.assertEqual(d["summary"]["changed"], 0)
        self.assertEqual(d["summary"]["added"], 0)

    def test_enum_change_is_detailed(self):
        d = self.drift(self.baseline(), enum=("Queued", "Running", "Failed"))
        self.assertEqual(d["summary"]["changed"], 1)
        ch = d["changed"][0]["changes"]["enums"]
        self.assertEqual(list(ch.values())[0], {"added": ["Failed"], "removed": []})

    def test_added_and_baseline_only(self):
        base = self.baseline()
        del base["paths"]["/api/scans/{id}"]
        d = self.drift(base)
        self.assertEqual([r["key"] for r in d["added"]], ["GET /api/scans/{id}"])
        self.assertEqual([r["key"] for r in d["baseline_only"]], ["GET /api/gone"])

    def test_header_params_that_oas_ignores_do_not_count(self):
        base = self.baseline(extra=False)
        base["paths"]["/api/scans"]["get"]["parameters"].append(
            {"name": "Authorization", "in": "header", "required": True, "schema": {"type": "string"}})
        self.assertEqual(self.drift(base)["summary"]["changed"], 0)

    def test_malformed_baseline_security_is_treated_as_auth(self):
        base = self.baseline(extra=False)
        base["paths"]["/api/scans"]["get"]["security"] = [[{"key": "Login", "type": "oauth2"}]]
        self.assertEqual(self.drift(base)["summary"]["changed"], 0)

    def test_used_endpoints_trigger(self):
        base = self.baseline(extra=True)
        res = build([src("live", "SCANS", live_doc(enum=("Queued",)))], [])
        used = [(None, A.norm_path("/api/scans")), ("GET", "/api/gone")]
        d = A.compute_drift(res, base, A.update_history({}, res["live_sigs"], DATE), used)
        self.assertEqual({(a["key"], a["category"]) for a in d["used_endpoints"]["affected"]},
                         {("GET /api/scans", "changed"), ("GET /api/gone", "baseline_only")})

    def test_removed_candidate_needs_two_missing_runs(self):
        res = build([src("live", "SCANS", live_doc())], [])
        h1 = A.update_history({}, res["live_sigs"], "2026-01-01")
        gone = {k: v for k, v in res["live_sigs"].items() if k[1] != "/api/scans/{}"}
        h2 = A.update_history(h1, gone, "2026-01-08")
        self.assertEqual(h2["ops"]["GET /api/scans/{}"]["missing_runs"], 1)
        h3 = A.update_history(h2, gone, "2026-01-15")
        self.assertEqual(h3["ops"]["GET /api/scans/{}"]["missing_runs"], 2)
        d = A.compute_drift(res, None, h3, [])
        self.assertEqual([r["key"] for r in d["removed_candidate"]], ["GET /api/scans/{}"])
        self.assertEqual(A.compute_drift(res, None, h2, [])["removed_candidate"], [])

    def test_live_verified_is_stable_until_schema_changes(self):
        res = build([src("live", "SCANS", live_doc())], [])
        hist = A.update_history({}, res["live_sigs"], "2026-01-01")
        same = [src("live", "SCANS", live_doc())]
        later = A.build_spec(same, [], [], [], hist, *A.learn_prefixes(same, []), "2026-02-01")
        self.assertEqual(later["doc"]["paths"]["/api/scans"]["get"]["x-live-verified"], "2026-01-01")
        new = [src("live", "SCANS", live_doc(enum=("Queued",)))]
        changed = A.build_spec(new, [], [], [], hist, *A.learn_prefixes(new, []), "2026-02-01")
        self.assertEqual(changed["doc"]["paths"]["/api/scans"]["get"]["x-live-verified"], "2026-02-01")


class Repairs(unittest.TestCase):
    def test_known_upstream_slips_are_repaired_and_logged(self):
        doc = {"paths": {"/x/{id}": {"get": {
            "requestBody": None,
            "parameters": [
                {"name": "limit", "in": "query", "type": "integer", "default": 10},
                {"name": "ghost", "in": "path", "required": True, "schema": {"type": "string"}}],
            "responses": {"200": {"description": "ok", "content": {"application/json": {"schema": {
                "type": "object", "optional": True, "properties": {"n": {"type": "integer", "default": "0"}}},
                "examples": {"a": {"title": "A", "value": 1}}}}}}}}}}
        log = A.repair_doc(doc)
        op = doc["paths"]["/x/{id}"]["get"]
        self.assertNotIn("requestBody", op)
        self.assertEqual(op["parameters"][0]["schema"], {"type": "integer", "default": 10})
        self.assertEqual([p["name"] for p in op["parameters"]], ["limit"])           # ghost not in template
        media = op["responses"]["200"]["content"]["application/json"]
        self.assertNotIn("optional", media["schema"])
        self.assertNotIn("default", media["schema"]["properties"]["n"])
        self.assertEqual(media["examples"]["a"], {"summary": "A", "value": 1})
        self.assertGreaterEqual(len(log), 5)


class Validation(unittest.TestCase):
    def doc(self):
        return build([src("live", "SCANS", live_doc())], [src("stoplight", "s1", sl_doc())])["doc"]

    def test_validator_runs_by_default(self):
        out = A.validate_openapi(self.doc())
        self.assertEqual(out["validator"], "ok")
        self.assertIn("passes the OpenAPI 3.0 validator", out["summary"])

    def test_validator_reports_errors(self):
        doc = self.doc()
        doc["paths"]["/api/scans"]["get"]["responses"] = "nope"
        out = A.validate_openapi(doc)
        self.assertEqual(out["validator"], "errors")
        self.assertGreaterEqual(out["validator_error_count"], 1)

    def test_skip_flag(self):
        out = A.validate_openapi(self.doc(), skip=True)
        self.assertEqual(out["validator"], "skipped")
        self.assertIn("--skip-validation", out["summary"])

    def test_cli_flag_and_default(self):
        import argparse
        ap = argparse.ArgumentParser()
        A.add_arguments(ap)
        self.assertFalse(A.build_cfg(ap.parse_args([])).skip_validation)
        self.assertTrue(A.build_cfg(ap.parse_args(["--skip-validation"])).skip_validation)

    def test_missing_validator_stops_the_run_before_fetching(self):
        import argparse
        from unittest import mock
        ap = argparse.ArgumentParser()
        A.add_arguments(ap)
        cfg = A.build_cfg(ap.parse_args([]))
        with mock.patch.object(A, "_load_validator", side_effect=A.FatalError("missing")), \
                mock.patch.object(A, "fetch_all") as fetch:
            self.assertEqual(A.run(cfg), A.EXIT_FATAL)
            fetch.assert_not_called()


class LoadUsed(unittest.TestCase):
    def test_formats(self):
        txt = temp_file("# c\nGET /api/a/{x}\n/api/b\n", ".txt")
        js = temp_file(json.dumps({"endpoints": ["post /api/c"]}), ".json")
        self.addCleanup(os.remove, txt)
        self.addCleanup(os.remove, js)
        self.assertEqual(A.load_used(txt), [("GET", "/api/a/{}"), (None, "/api/b")])
        self.assertEqual(A.load_used(js), [("POST", "/api/c")])
        with self.assertRaises(A.FatalError):
            A.load_used(txt + ".missing")


if __name__ == "__main__":
    unittest.main()
