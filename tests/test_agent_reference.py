"""Agent-reference cleanup: accordions, toggle text, section pages, CxQL part,
page header and the page index.

    python -m unittest discover -s tests -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cx_docs_mirror as D  # noqa: E402
import cx_docs_render as R  # noqa: E402
from test_docs_render import render, rec, section  # noqa: E402
from test_output_layout import docs_args  # noqa: E402
from test_placement import read_text, write_json  # noqa: E402

ACCORDION = ('<div class="sidebar accordion"><div class="accordion-icon"></div><div class="panel">'
             '<div class="panel-heading"><div class="titlepage"><a><div class="sidebar-title">Add(Int32)</div></a>'
             '</div></div><div class="panel-body collapse"><p>Adds a node.</p><p>Expand source</p>'
             '<pre>int a;</pre></div></div></div>')


class Accordions(unittest.TestCase):
    def test_accordion_is_content_not_a_blockquote(self):
        md = render(ACCORDION)
        self.assertNotIn(">", md.replace("->", ""))
        self.assertIn("**Add(Int32)**", md)
        self.assertIn("Adds a node.", md)
        self.assertIn("int a;", md)

    def test_toggle_text_is_removed(self):
        md = render("<p>Keep</p><p>Expand source</p><p>Collapse source</p>")
        self.assertEqual(md, "Keep")

    def test_plain_sidebar_stays_a_callout(self):
        md = render('<div class="sidebar"><div class="sidebar-title">Tip box</div><p>Careful.</p></div>')
        self.assertIn("> **Tip box:**", md)

    def test_admonition_stays_a_callout(self):
        self.assertIn("> **Note:**", render('<div class="note"><p>Read this.</p></div>'))

    def test_metrics_report_quote_share_and_toggles(self):
        m = R.quality_metrics("### A {#p-1}\n> quoted\nExpand source\n> Expand source\n```\nExpand source\n```", D.slugify)
        self.assertEqual(m["toggle_leftovers"], 2)
        self.assertGreater(m["blockquote_pct"], 0)


class Shells(unittest.TestCase):
    def pages(self):
        parent = rec("34965-1-p.html", "Reports", ["P", "Reports"], nav=1, _section=section(""), shell=True)
        child = rec("34965-2-c.html", "Daily", ["P", "Reports", "Daily"], nav=3, _section=section("<p>x</p>"))
        late = rec("34965-3-l.html", "Weekly", ["P", "Reports", "Weekly"], nav=2, _section=section("<p>y</p>"))
        other = rec("34965-4-o.html", "Other", ["P", "Other"], nav=4, _section=section("<p>z</p>"))
        return parent, child, late, other

    def test_empty_page_with_children_is_kept_as_a_shell(self):
        parent, child, late, other = self.pages()
        parent.pop("shell")
        index = {m["file"]: m for m in (parent, child, late, other)}
        self.assertEqual(D._drop_empty(index, "strip"), 0)
        self.assertTrue(parent["shell"])
        self.assertEqual(parent["status"], "keep")

    def test_title_only_leaf_is_dropped(self):
        leaf = rec("34965-9-t.html", "Older Versions", ["P", "Older Versions"],
                   _section=section("<div><h1>Older Versions</h1></div><p></p>"))
        self.assertEqual(D._drop_empty({leaf["file"]: leaf}, "strip"), 1)
        self.assertEqual(leaf["drop_reason"], "empty")

    def test_shell_lists_children_in_navigation_order(self):
        parent, child, late, other = self.pages()
        index = {m["file"]: m for m in (parent, child, late, other)}
        records, stats = D._build_records(index, D.build_cfg(docs_args()))
        md = next(r for r in records if r["file"] == parent["file"])["markdown"]
        self.assertLess(md.index("Weekly"), md.index("Daily"))
        self.assertNotIn("Other", md)
        self.assertIn("(#p-3)", md)
        self.assertEqual(stats["shell_pages"], 1)

    def test_shell_without_surviving_children_is_dropped(self):
        parent, child, late, other = self.pages()
        child["status"] = late["status"] = "drop"
        index = {m["file"]: m for m in (parent, child, late, other)}
        self.assertEqual(D._resolve_shells(index), 1)
        self.assertEqual(parent["status"], "drop")


class Parts(unittest.TestCase):
    def test_cxql_page_is_tagged_independently_of_api(self):
        a = rec("34965-1-q.html", "SAST Query Language APIs", ["P", "SAST Query Language APIs"])
        b = rec("34965-2-e.html", "Endpoints", ["P", "Checkmarx One API Documentation"])
        c = rec("34965-3-g.html", "Guide", ["P"])
        index = {m["file"]: m for m in (a, b, c)}
        self.assertEqual(D._tag_parts(index, "separate", D.API_REFERENCE_CRUMBS, "api"), 1)
        self.assertEqual(D._tag_parts(index, "separate", D.CXQL_REFERENCE_PATTERNS, "cxql"), 1)
        self.assertEqual((a["part"], b["part"], c["part"]), ("cxql", "api", "main"))
        D._tag_parts(index, "inline", D.CXQL_REFERENCE_PATTERNS, "cxql")
        self.assertEqual((a["part"], b["part"]), ("main", "api"))


class CombineOutputs(unittest.TestCase):
    def setUp(self):
        self.root = os.path.abspath(os.path.normpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.extract = os.path.join(self.root, "x.json")
        self.md = os.path.join(self.root, "out.md")

    def page(self, n, title, nav, part="main", modified="", crumbs=("P",), **kw):
        r = {"file": f"34965-{n}-t.html", "title": title, "product": "P", "breadcrumb": list(crumbs),
             "modified": modified, "status": "keep", "markdown": "body", "part": part,
             "anchor": f"p-{n}", "nav": nav, "topic_id": str(n), "display_title": title}
        r.update(kw)
        return r

    def combine(self, pages):
        write_json(self.extract, pages)
        cfg = D.build_cfg(docs_args("--json-path", self.extract, "--md-path", self.md))
        D.stage_combine(cfg)
        return cfg

    def test_cxql_goes_to_its_own_file_with_a_pointer(self):
        cfg = self.combine([self.page(1, "Guide", 0), self.page(2, "Methods", 1, part="cxql")])
        text = read_text(cfg.md_path)
        part = read_text(D._part_path(cfg.md_path, "cxql-reference"))
        self.assertNotIn("### Methods", text)
        self.assertIn("### Methods {#p-2}", part)
        self.assertIn("1 CxQL-reference page is in", text)

    def test_header_is_one_key_per_line_with_flags(self):
        old = datetime.now().replace(year=datetime.now().year - 4).strftime("%B %d, %Y")
        cfg = self.combine([self.page(1, "Old", 0, modified=old, crumbs=("P", "Upcoming Features", "Old"))])
        text = read_text(cfg.md_path)
        block = text.split("### Old {#p-1}")[1].split("\n\n")[1]
        self.assertEqual(block.split("  \n")[0], "Path: P › Upcoming Features › Old")
        self.assertIn(f"Modified: {old}", block)
        self.assertIn("Source: 34965-1-t.html", block)
        self.assertIn("Flags: stale, upcoming-section", block)

    def test_index_lists_every_page_with_its_file(self):
        cfg = self.combine([self.page(1, "Guide", 0, modified="March 2, 2026"),
                            self.page(2, "Methods", 1, part="cxql", aliases=["34965-9-dup.html"])])
        idx = json.loads(read_text(D._index_path(cfg.md_path)))
        by_id = {p["id"]: p for p in idx["pages"]}
        self.assertEqual(set(by_id), {"p-1", "p-2"})
        self.assertEqual(by_id["p-1"]["file"], os.path.basename(cfg.md_path))
        self.assertEqual(by_id["p-1"]["modified"], "2026-03-02")
        self.assertEqual(by_id["p-2"]["part"], "cxql-reference")
        self.assertEqual(by_id["p-2"]["aliases"], ["34965-9-dup.html"])
        self.assertEqual(idx["files"]["cxql-reference"],
                         os.path.basename(D._part_path(cfg.md_path, "cxql-reference")))
        self.assertGreater(by_id["p-1"]["bytes"], 0)


if __name__ == "__main__":
    unittest.main()
