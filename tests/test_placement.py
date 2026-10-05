"""Placement must not depend on the baseline; drift tiers; overlay keep flag;
opt-in authenticated fetch. Run from the repo root:

    python -m unittest discover -s tests -v
"""

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cx_api_spec as A  # noqa: E402


def op(summary="", **extra):
    return {"summary": summary, "responses": {"200": {"description": "ok"}}, **extra}


# CONTRIBUTORS is served at /api/contributors; Scanners Results at /api/results.
# Both have a bare "GET /" with the same summary, which is the trap.
CONTRIBUTORS = {
    "openapi": "3.0.2", "servers": [{"url": "/api/contributors"}],
    "paths": {"/": {"get": op("Retrieve list")}, "/csv": {"get": op("Export csv")},
              "/insights": {"get": op("Insights")}},
}
RESULTS_SL = {
    "openapi": "3.0.0",
    "info": {"title": "Scanners Results", "description": "<b>{Base_URL}/api/results</b>"},
    "paths": {"/": {"get": op("Retrieve list")}, "/summary": {"get": op("Summary")}},
}


def norm(path: str) -> str:
    return os.path.abspath(os.path.normpath(path))


def scratch() -> str:
    return norm(tempfile.mkdtemp())


def write_text(path: str, text: str) -> None:
    with open(norm(path), "w", encoding="utf-8") as fh:
        fh.write(text)


def write_json(path: str, obj) -> None:
    write_text(path, json.dumps(obj))


def read_text(path: str) -> str:
    with open(norm(path), "r", encoding="utf-8") as fh:
        return fh.read()


def make_raw(root: str) -> str:
    """A saved raw/ directory holding the two documents above."""
    rs = A.RawSet()
    rows = [{"name": "Contributors", "key": "CONTRIBUTORS", "url": "/spec/c.yaml",
             "file": "live/CONTRIBUTORS.yaml", "source_url": "https://ast.example.test/spec/c.yaml"}]
    rs.put("live/catalog.json", json.dumps(rows).encode(), "https://ast.example.test/x")
    rs.put("live/CONTRIBUTORS.yaml", json.dumps(CONTRIBUTORS).encode(), "https://ast.example.test/c")
    svc = [{"id": "s1", "title": "Scanners Results", "slug": "s1", "uri": "/r.yaml", "file": "r.yaml",
            "export_url": "https://stoplight.io/x", "relpath": "stoplight/s1__r.yaml"}]
    rs.put("stoplight/services.json", json.dumps(svc).encode(), "https://stoplight.io/x")
    rs.put("stoplight/s1__r.yaml", json.dumps(RESULTS_SL).encode(), "https://stoplight.io/x")
    raw_dir = os.path.join(root, "raw")
    rs.save(Path(raw_dir))
    return raw_dir


def cfg_for(root: str, raw_dir: str, out: str, baseline=None):
    empty_overlay = os.path.join(root, "no-overlay")
    os.makedirs(empty_overlay, exist_ok=True)
    return SimpleNamespace(
        base_url="https://ast.example.test", extra_services=[], out_dir=os.path.join(root, out),
        overlay_dir=empty_overlay, baseline=baseline, used_endpoints=None, dry_run=False,
        from_raw=raw_dir, probe=False, run_date="2026-01-01", skip_validation=True,
        auth_token_env=None, auth_token=None, concurrency=4, timeout=5, retries=1,
        allowed_hosts={"ast.example.test"})


class PlacementIgnoresBaseline(unittest.TestCase):
    def setUp(self):
        self.root = scratch()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.raw = make_raw(self.root)
        # the baseline has GET /api/results, which CONTRIBUTORS must never be placed under
        self.baseline = os.path.join(self.root, "baseline.json")
        write_json(self.baseline, {"openapi": "3.0.3", "info": {"title": "b", "version": "1"},
                                   "paths": {"/api/results": {"get": op("Retrieve list")}}})

    def run_stage(self, out, baseline=None):
        self.assertEqual(A.run(cfg_for(self.root, self.raw, out, baseline)), 0)
        return read_text(os.path.join(self.root, out, "cxone_openapi.json"))

    def test_same_spec_with_and_without_baseline(self):
        self.assertEqual(self.run_stage("a"), self.run_stage("b", self.baseline))

    def test_contributors_never_lands_under_api_results(self):
        spec = json.loads(self.run_stage("c", self.baseline))
        for path, item in spec["paths"].items():
            for o in item.values():
                if o.get("x-service") == "Contributors":
                    self.assertTrue(path.startswith("/api/contributors"), path)
        self.assertEqual(spec["paths"]["/api/results"]["get"]["x-service"], "Scanners Results")
        self.assertNotIn("/api/results/csv", spec["paths"])
        self.assertNotIn("/api/results/insights", spec["paths"])


class StoplightEvidence(unittest.TestCase):
    def sl(self, paths, prefix="/api/results"):
        d = {"openapi": "3.0.0", "info": {"title": "S", "description": f"<b>{{Base_URL}}{prefix}</b>"},
             "paths": paths}
        s = A.Source("stoplight", "s1", "S", "s.yaml", "s.yaml", d)
        s.prefix, s.prefix_src, s.notes = A._stoplight_prefix(d)
        return s

    def live(self, paths, url="/api/contributors"):
        return A.Source("live", "L", "L", "l.yaml", "l.yaml", {"paths": paths}, url, "x")

    def test_bare_slash_and_id_matches_are_not_evidence(self):
        L = self.live({"/": {"get": op("a")}, "/{id}": {"get": op("a")}})
        info, _ = A.learn_prefixes([L], [self.sl({"/": {"get": op("a")}, "/{x}": {"get": op("a")}})])
        self.assertEqual(info["L"]["confidence"], "declared")
        self.assertIsNone(info["L"]["stoplight"])

    def test_one_real_match_is_not_enough(self):
        L = self.live({"/csv": {"get": op("a")}})
        info, _ = A.learn_prefixes([L], [self.sl({"/csv": {"get": op("a")}})])
        self.assertEqual(info["L"]["confidence"], "declared")

    def test_several_matches_corroborate_or_conflict(self):
        both = {"/csv": {"get": op("a")}, "/summary": {"get": op("b")}}
        agree, _ = A.learn_prefixes([self.live(both, "/api/results")], [self.sl(both)])
        self.assertEqual(agree["L"]["confidence"], "matched")
        clash, _ = A.learn_prefixes([self.live(both)], [self.sl(both)])
        self.assertEqual(clash["L"]["confidence"], "conflict")
        self.assertEqual(clash["L"]["prefix"], "/api/contributors")     # live wins; a human decides

    def test_unusable_live_prefix_is_not_guessed_from_weak_evidence(self):
        L = self.live({"/": {"get": op("a")}}, url=None)
        info, _ = A.learn_prefixes([L], [self.sl({"/": {"get": op("a")}})])
        self.assertEqual(info["L"]["confidence"], "unknown")
        self.assertIsNone(info["L"]["prefix"])


class History(unittest.TestCase):
    def test_new_history_is_versioned(self):
        self.assertEqual(A.update_history({}, {}, "2026-01-01")["version"], A.HISTORY_VERSION)


class Severity(unittest.TestCase):
    def facts(self, **kw):
        base = {"parameters": {}, "request": {}, "responses": {}, "enums": {}, "required": {}, "auth": "none"}
        base.update(kw)
        return base

    def classify(self, old, new):
        return A.classify_change(old, new, A.compare_facts(old, new))

    def test_additive_changes(self):
        old = self.facts()
        new = self.facts(parameters={"query:x": {"schema": {"type": "string"}}},
                         required={"param/query:x": False},
                         responses={"200:application/json": {"properties": {"n": {"type": "integer"}}}})
        self.assertEqual(self.classify(old, new), [])

    def test_breaking_changes(self):
        p = {"query:x": {"schema": {"type": "string"}}}
        self.assertTrue(self.classify(self.facts(parameters=p), self.facts()))                       # removed parameter
        self.assertTrue(self.classify(self.facts(parameters=p, required={"param/query:x": False}),
                                      self.facts(parameters=p, required={"param/query:x": True})))   # now required
        self.assertTrue(self.classify(self.facts(enums={"param/query:x": ["a", "b"]}),
                                      self.facts(enums={"param/query:x": ["a"]})))                   # enum value removed
        self.assertTrue(self.classify(self.facts(parameters=p),
                                      self.facts(parameters={"query:x": {"schema": {"type": "integer"}}})))
        self.assertTrue(self.classify(self.facts(auth="none"), self.facts(auth=["x"])))
        self.assertTrue(self.classify(
            self.facts(responses={"200:j": {"properties": {"a": {"type": "string"}}}}),
            self.facts(responses={"200:j": {"properties": {}}})))                                    # field removed

    def test_not_breaking(self):
        self.assertEqual(self.classify(self.facts(enums={"param/query:x": ["a", "b"]}), self.facts()), [])  # enum dropped
        self.assertEqual(self.classify(
            self.facts(parameters={"header:CorrelationId": {"schema": {}}}), self.facts()), [])             # optional header
        self.assertEqual(self.classify(
            self.facts(responses={"400:j": {"properties": {"a": {"type": "string"}}}}),
            self.facts(responses={"400:j": {}})), [])                                                       # error body
        self.assertEqual(self.classify(
            self.facts(responses={"200:j": {"properties": {"a": {"maximum": 5}}}}),
            self.facts(responses={"200:j": {"properties": {"a": {}}}})), [])                                # constraint


class UsedEndpointExit(unittest.TestCase):
    def setUp(self):
        self.root = scratch()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.raw = make_raw(self.root)
        self.base = os.path.join(self.root, "base.json")
        write_json(self.base, {"openapi": "3.0.3", "info": {"title": "b", "version": "1"},
                               "security": [{"B": []}],
                               "components": {"securitySchemes": {"B": {"type": "http", "scheme": "bearer"}}},
                               "paths": {
            "/api/gone": {"get": op("x")},
            "/api/contributors/csv": {"get": op("Export csv")}}})

    def exit_code(self, used_line):
        used = os.path.join(self.root, "used.txt")
        write_text(used, used_line)
        cfg = cfg_for(self.root, self.raw, "u", self.base)
        cfg.used_endpoints = used
        return A.run(cfg)

    def test_unchanged_used_endpoint_exits_zero(self):
        self.assertEqual(self.exit_code("GET /api/contributors/csv\n"), 0)

    def test_missing_used_endpoint_exits_ten(self):
        self.assertEqual(self.exit_code("GET /api/gone\n"), A.EXIT_USED_DRIFT)


class OverlayKeep(unittest.TestCase):
    def test_keep_flag_suppresses_retire_message(self):
        live = A.Source("live", "L", "L", "l.yaml", "l.yaml",
                        {"paths": {"/a": {"get": op("a")}, "/b": {"get": op("b")}}}, "/api/x", "x")
        ov = A.Source("overlay", "o", "o", "o.json", "o.json", {"paths": {
            "/api/x/a": {"get": op("kept", **{"x-overlay-keep": True})},
            "/api/x/b": {"get": op("retire")}}})
        info, sl = A.learn_prefixes([live], [])
        res = A.build_spec([live], [], [], [ov], {}, info, sl, "2026-01-01")
        self.assertEqual([c["key"] for c in res["overlay_covered"]], ["GET /api/x/b"])


class AuthenticatedFetch(unittest.TestCase):
    class FakeClient:
        def __init__(self):
            self.calls = []

        async def get(self, url, timeout=None, headers=None, follow_redirects=True):
            self.calls.append((url, headers, follow_redirects))
            return SimpleNamespace(status_code=200, content=b"{}", headers={})

    def get(self, headers):
        cfg = SimpleNamespace(retries=1, timeout=5, allowed_hosts={"ast.example.test"})
        client = self.FakeClient()
        asyncio.run(A._get(client, asyncio.Semaphore(1), "https://ast.example.test/x", cfg, headers))
        return client.calls[0]

    def test_credentials_never_follow_redirects(self):
        _, headers, follow = self.get({"Authorization": "Bearer t"})
        self.assertEqual(headers, {"Authorization": "Bearer t"})
        self.assertFalse(follow)

    def test_plain_requests_carry_no_credentials(self):
        _, headers, follow = self.get(None)
        self.assertIsNone(headers)
        self.assertTrue(follow)

    def test_token_is_never_sent_to_another_host(self):
        cfg = SimpleNamespace(retries=1, timeout=5, allowed_hosts={"ast.example.test"})
        client = self.FakeClient()
        r, _ = asyncio.run(A._get(client, asyncio.Semaphore(1), "https://evil.example/x", cfg,
                                  {"Authorization": "Bearer t"}))
        self.assertIsNone(r)
        self.assertEqual(client.calls, [])

    def test_missing_env_var_is_a_clear_error(self):
        root = scratch()
        self.addCleanup(shutil.rmtree, root, True)
        cfg = cfg_for(root, "", "x")
        cfg.from_raw, cfg.auth_token_env = None, "CX_TEST_TOKEN_THAT_IS_NOT_SET"
        os.environ.pop(cfg.auth_token_env, None)
        self.assertEqual(A.run(cfg), A.EXIT_FATAL)


if __name__ == "__main__":
    unittest.main()
