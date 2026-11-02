"""Structure analysis, enrichment checks and the compact views.

    python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cx_docs_analysis as A  # noqa: E402
import cx_docs_enrich as E  # noqa: E402
import cx_docs_mirror as D  # noqa: E402
import cx_docs_views as V  # noqa: E402
from test_output_layout import docs_args  # noqa: E402
from test_placement import read_text, write_json  # noqa: E402

SETUP = ("To install the extension open the marketplace, search for the Checkmarx extension and press install. "
         "Then sign in with your API key and choose a tenant so that scans can run from the editor. ")


def body(editor: str, extra: str = "") -> str:
    return (f"Install the {editor} extension\n\n" + SETUP * 3 + f"\n\nIn {editor} choose Settings and then Extensions. "
            + extra)


class Layers(unittest.TestCase):
    """Builds a tiny combined guide on disk through the real combine stage."""

    def setUp(self):
        self.root = os.path.abspath(os.path.normpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.md = os.path.join(self.root, "guide.md")
        self.spec = os.path.join(self.root, "spec.json")
        write_json(self.spec, {"paths": {"/api/scans": {}, "/api/scans/{id}": {}, "/projects/{id}/tags": {}}})
        pages = [
            self.page(1, "Developer Assist for VS Code", 0, body("VS Code"), ["P", "Dev Assist", "VS Code"]),
            self.page(2, "Developer Assist for Cursor", 1, body("Cursor"), ["P", "Dev Assist", "Cursor"]),
            self.page(3, "Unrelated Reports", 2,
                      "Reports summarise findings per project. Use the reports page to export a CSV file for audit. "
                      "Call /api/scans/{scan_id} and /api/missing/{id} or /projects/{id}/tags to automate it. "
                      "The page also explains severity levels, filters and the date range used by every report "
                      "that you generate.\n\n```\ncx results show --report-format pdf\ncx scan create -s . --project-name x\n```",
                      ["P", "Reports"]),
            self.page(4, "Custom States", 3, "Custom states allow flexible triage of results in your tenant. " * 6,
                      ["P", "Upcoming Features", "Custom States"]),
            self.page(5, "Adding Custom States via the UI", 4, "Custom states allow flexible triage of results in your tenant. " * 6,
                      ["P", "Triage", "Adding Custom States via the UI"]),
            self.page(6, "Section", 5, "_This section page has no text of its own. Pages in this section:_\n\n- [A](#p-1)",
                      ["P", "Section"], shell=True),
        ]
        write_json(os.path.join(self.root, "x.json"), pages)
        cfg = D.build_cfg(docs_args("--json-path", os.path.join(self.root, "x.json"), "--md-path", self.md))
        D.stage_combine(cfg)
        self.guide = cfg.md_path

    def page(self, n, title, nav, markdown, crumbs, **kw):
        r = {"file": f"34965-{n}-t.html", "title": title, "product": "P", "breadcrumb": crumbs, "modified": "",
             "status": "keep", "markdown": markdown, "part": "main", "anchor": f"p-{n}", "nav": nav,
             "topic_id": str(n), "display_title": title}
        r.update(kw)
        return r

    # --- analysis -------------------------------------------------------------------------------

    def test_load_pages_reads_headers_and_bodies_in_guide_order(self):
        _, pages = A.load_pages(self.guide)
        self.assertEqual([p["id"] for p in pages], [f"p-{i}" for i in range(1, 7)])
        self.assertEqual(pages[0]["header"]["Source"], "34965-1-t.html")
        self.assertTrue(pages[0]["body"].startswith("Install the VS Code extension"))
        self.assertIn("upcoming-section", pages[3]["flags"])

    def test_variants_cluster_and_unrelated_pages_do_not(self):
        _, pages = A.load_pages(self.guide)
        clusters = A.find_clusters(pages)["clusters"]
        ids = [sorted(m["id"] for m in c["members"]) for c in clusters]
        self.assertIn(["p-1", "p-2"], ids)
        self.assertTrue(all("p-3" not in i for i in ids))
        self.assertIn("p-4", sum(ids, []))                          # identical text counts as a cluster too

    def test_analysis_is_deterministic_and_writes_reports(self):
        first = A.run(self.guide, self.spec)
        text = read_text(f"{os.path.splitext(self.guide)[0]}-clusters.json")
        self.assertEqual(A.run(self.guide, self.spec), first)
        self.assertEqual(read_text(f"{os.path.splitext(self.guide)[0]}-clusters.json"), text)
        self.assertIn("structure review", read_text(f"{os.path.splitext(self.guide)[0]}-review.md"))

    def test_upcoming_page_that_also_exists_is_found(self):
        _, pages = A.load_pages(self.guide)
        found = A.upcoming_findings(pages)
        self.assertEqual([(f["upcoming"], f["also"]) for f in found], [("p-4", "p-5")])

    def test_api_paths_are_checked_against_the_spec(self):
        _, pages = A.load_pages(self.guide)
        got = {r["path"]: r["status"] for r in A.api_path_findings(pages, self.spec)["mentions"]}
        self.assertEqual(got["/api/scans/{}"], "in spec")
        self.assertEqual(got["/projects/{}/tags"], "in spec")
        self.assertEqual(got["/api/missing/{}"], "not in spec")

    # --- enrichment -----------------------------------------------------------------------------

    def good(self, pid="p-3", **kw):
        rec = {"id": pid, "type": "task", "summary": "Explains how to export reports as a CSV file and automate it with /api/scans/{scan_id}.",
               "aliases": [], "ui_path": "", "cli": [], "api_paths": []}
        rec.update(kw)
        return rec

    def validate(self, rec, pid="p-3"):
        _, pages = A.load_pages(self.guide)
        by_id = {p["id"]: p for p in pages}
        variants = E.variant_map(pages)
        return E.validate(rec, by_id.get(pid), variants, set(), by_id.get(variants.get(pid, "")))

    def test_a_faithful_record_passes(self):
        self.assertEqual(self.validate(self.good(cli=["cx scan create"], api_paths=["/api/scans/{id}"])), [])

    def test_invented_numbers_names_and_commands_are_rejected(self):
        for rec in (self.good(summary="Exports reports and keeps the last 90 days of data for every project."),
                    self.good(summary="Uses the QuantumScan engine to export reports for every project here."),
                    self.good(cli=["cx scan delete"]),
                    self.good(api_paths=["/api/other/{id}"]),
                    self.good(ui_path="Settings > Billing"),
                    self.good(aliases=["Report Hub"]),
                    self.good(type="essay"),
                    self.good(summary="Too short.")):
            self.assertTrue(self.validate(rec), rec)

    def test_paths_written_as_markdown_links_still_match(self):
        page = {"title": "Reports", "path": ["P"], "body": "Use GET **{Base\_URL}**/[api/scans](https://x.y/doc) now."}
        rec = self.good(summary="Lists scans with GET /api/scans on the base URL for every report.",
                        api_paths=["/api/scans"])
        self.assertEqual(E.validate(rec, dict(page, id="p-3"), {}, set()), [])

    def test_variant_delta_only_for_variants(self):
        self.assertTrue(self.validate(self.good(variant_delta="Differs by editor.")))
        rec = self.good("p-2", summary="Explains how to install the Cursor extension and sign in with an API key.",
                        variant_delta="Uses Cursor instead of VS Code.")
        self.assertEqual(self.validate(rec, "p-2"), [])

    def test_synonyms_file_allows_curated_aliases(self):
        _, pages = A.load_pages(self.guide)
        rec = self.good(aliases=["Report Hub"])
        self.assertEqual(E.validate(rec, pages[2], {}, {"report hub"}), [])

    def test_prep_check_cycle_is_cached(self):
        r = E.prep(self.guide)
        self.assertGreaterEqual(r["to_enrich"], 3)
        self.assertEqual(r["stubs"], 1)                              # the section page needs no LLM
        work = E.paths_for(self.guide)["work"]
        batch = json.loads(read_text(os.path.join(work, "batch-001.json")))
        ids = [p["id"] for p in batch["pages"]]
        variant = next(p for p in batch["pages"] if "variant_of" in p)
        self.assertTrue(variant["unique_to_page"])
        records = [self.good("p-3"),
                   self.good("p-1", summary="Explains how to install the VS Code extension and sign in with an API key."),
                   self.good("p-2", summary="Explains how to install the Cursor extension and sign in with an API key.",
                             variant_delta="Uses Cursor instead of VS Code."),
                   self.good("p-4", summary="Describes custom states for flexible triage of results in a tenant."),
                   self.good("p-5", summary="Describes custom states for flexible triage of results in a tenant, via the UI."),
                   self.good("p-9")]
        records[-1]["summary"] = "Invented a 42 day retention for something that is not there at all."
        write_json(os.path.join(work, "result-001.json"), [r for r in records if r["id"] in ids or r["id"] == "p-9"])
        out = E.check(self.guide)
        self.assertEqual(out["rejected"], 1)                          # unknown page id
        self.assertEqual(out["stored"], len([r for r in records if r["id"] in ids]))
        companion = E.load_companion(self.guide)
        self.assertEqual(companion["p-6"]["type"], "section")         # the stub
        self.assertEqual(companion["p-2"]["variant_of"], "p-1")       # from the clusters, not the agent
        # a second run with the same pages and prompt needs no LLM and gives the same companion
        before = read_text(E.paths_for(self.guide)["companion"])
        again = E.prep(self.guide)
        self.assertEqual(again["to_enrich"], len(ids) - out["stored"])
        self.assertEqual(E.check(self.guide)["companion_records"], out["companion_records"])
        self.assertEqual(read_text(E.paths_for(self.guide)["companion"]), before)

    def test_changed_page_text_invalidates_its_record(self):
        _, pages = A.load_pages(self.guide)
        by_id = {p["id"]: p for p in pages}
        variants = E.variant_map(pages)
        key = E.page_key(by_id["p-3"], by_id, variants)
        by_id["p-3"] = dict(by_id["p-3"], body=by_id["p-3"]["body"] + " More text.")
        self.assertNotEqual(key, E.page_key(by_id["p-3"], by_id, variants))
        self.assertFalse(E.is_current({"key": key, "prompt_v": E.PROMPT_VERSION - 1}, key))

    # --- views ----------------------------------------------------------------------------------

    def test_reference_view_groups_variants_and_links_resolve(self):
        out = V.run(self.guide, self.spec, synonyms_file=os.path.join(self.root, "none.json"))
        self.assertEqual(out["dead_links"], 0)
        text = read_text(f"{os.path.splitext(self.guide)[0]}-reference.md")
        self.assertIn("- **Developer Assist for VS Code**", text)
        self.assertIn("  - also: Developer Assist for Cursor", text)
        self.assertNotIn("\n- **Developer Assist for Cursor**", text)
        self.assertIn("Reports summarise findings per project.", text)   # first-sentence fallback

    def test_lookup_lists_cli_commands_and_api_paths(self):
        V.run(self.guide, self.spec, synonyms_file=os.path.join(self.root, "none.json"))
        text = read_text(f"{os.path.splitext(self.guide)[0]}-lookup.md")
        self.assertIn("- `cx scan create` —", text)
        self.assertIn("- `cx results show` —", text)
        self.assertIn("- `/api/missing/{}` (not in spec)", text)

    def test_companion_summaries_are_marked_in_the_reference(self):
        E.prep(self.guide)
        work = E.paths_for(self.guide)["work"]
        write_json(os.path.join(work, "result-001.json"), [self.good("p-3")])
        E.check(self.guide)
        V.run(self.guide, self.spec, synonyms_file=os.path.join(self.root, "none.json"))
        text = read_text(f"{os.path.splitext(self.guide)[0]}-reference.md")
        self.assertIn("Explains how to export reports as a CSV file", text)
        self.assertIn("✓", text)

    def test_cli_command_extraction_stops_at_the_first_flag_or_argument(self):
        page = {"body": "```\n./cx scan create --project-name x\ncx utils env\nnot cx here\n```\ncx outside code\n"}
        self.assertEqual(V.cli_commands(page), {"cx scan create", "cx utils env"})


class StaleGuideWarningTests(unittest.TestCase):
    TODAY = date(2026, 11, 2)

    def test_old_guide_warns_with_age_and_the_refresh_commands(self):
        msg = D._stale_guide_warning("docs/checkmarx-one-docs-2026-10-20.md", 7, self.TODAY)
        self.assertIn("13 days old", msg)
        self.assertIn("--stage docs", msg)
        self.assertIn("--refresh", msg)

    def test_recent_guide_and_age_limit_zero_do_not_warn(self):
        self.assertEqual(D._stale_guide_warning("docs/checkmarx-one-docs-2026-10-30.md", 7, self.TODAY), "")
        self.assertEqual(D._stale_guide_warning("docs/checkmarx-one-docs-2026-01-01.md", 0, self.TODAY), "")

    def test_a_guide_name_without_a_date_is_never_flagged(self):
        self.assertIsNone(D._guide_age_days("docs/guide.md", self.TODAY))
        self.assertEqual(D._stale_guide_warning("docs/guide.md", 7, self.TODAY), "")


if __name__ == "__main__":
    unittest.main()
