"""Deterministic docs fixes: topic-id dedupe, headings, links, images, tables,
callouts, anchors, nav order, API split, staleness, quality metrics.

    python -m unittest discover -s tests -v
"""

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import cx_docs_mirror as D  # noqa: E402
import cx_docs_render as R  # noqa: E402
from test_output_layout import docs_args  # noqa: E402
from test_placement import read_text, write_json  # noqa: E402

BASE = "https://docs.checkmarx.com/en/"


def section(html: str):
    return BeautifulSoup(f"<section>{html}</section>", "lxml").find("section")


def render(html, images="strip", pages=None):
    """Markdown for an HTML fragment; `pages` maps file name -> (anchor, part)."""
    table = {k: {"anchor": a, "part": p} for k, (a, p) in (pages or {}).items()}
    return R.to_markdown(section(html), R.RenderCtx(BASE, images, table.get))


class Ids(unittest.TestCase):
    def test_topic_id_and_anchor(self):
        self.assertEqual(R.topic_id("34965-253661-sca-results.html"), "253661")
        self.assertEqual(R.anchor_for("34965-253661-sca-results.html"), "p-253661")
        self.assertEqual(R.anchor_for("index.html"), "p-index")

    def test_republished_slug_detected(self):
        self.assertTrue(R.is_reused_slug("34965-253661-sca-results-253661.html"))
        self.assertFalse(R.is_reused_slug("34965-253661-sca-results.html"))


class Images(unittest.TestCase):
    def test_alt_text_quality(self):
        for bad in ("", "6333663227.png", "Custom_Fields_Migrated_as_Tags_", "screen.PNG", "img-12ab"):
            self.assertFalse(R.meaningful_alt(bad), bad)
        self.assertTrue(R.meaningful_alt("The scan results table"))

    def test_strip_keeps_meaningful_alt_only(self):
        md = render('<p>See <img src="image/a.png" alt="Scan results table"/> and '
                    '<img src="image/b.png" alt="b.png"/>.</p>')
        self.assertIn("[Image: Scan results table]", md)
        self.assertNotIn("image/", md)
        self.assertNotIn("![", md)

    def test_absolute_mode_never_leaves_a_relative_path(self):
        md = render('<img src="image/a.png" alt="a.png"/>', images="absolute")
        self.assertIn(f"]({BASE}image/a.png", md)

    def test_link_wrapping_only_an_image_disappears(self):
        self.assertNotIn("[]", render('<p><a href="x.html"><img src="image/a.png" alt="a.png"/></a>Text</p>'))


class Callouts(unittest.TestCase):
    def test_note_becomes_blockquote_not_heading(self):
        md = render('<div class="note"><h3 class="title">Note</h3><p>Check this.</p></div>')
        self.assertEqual(md, "> **Note:** Check this.")

    def test_notice_class_pair_uses_the_title(self):
        md = render('<div class="note notice"><h3 class="title">Notice</h3><p>Hi.</p></div>')
        self.assertTrue(md.startswith("> **Notice:**"))

    def test_callout_with_a_list(self):
        md = render('<div class="tip"><h3 class="title">Tip</h3><ul><li><p>a</p></li><li><p>b</p></li></ul></div>')
        self.assertIn("> **Tip:**", md)
        self.assertIn("> - a", md)

    def test_sidebar(self):
        md = render('<div class="sidebar"><div class="titlepage"><div><div><div class="sidebar-title">'
                    'What happens next?</div></div></div></div><p>Go on.</p></div>')
        self.assertTrue(md.startswith("> **What happens next?**"), md)
        self.assertNotIn("sidebar", md)


class Tables(unittest.TestCase):
    def test_simple_table_stays_a_pipe_table(self):
        md = render("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>")
        self.assertIn("| A | B |", md)

    def test_block_content_in_a_cell_becomes_a_list(self):
        md = render("<table><tr><th>Name</th><th>Example</th></tr>"
                    "<tr><td>Query</td><td><pre>{\"a\": 1}</pre><p>second</p></td></tr></table>")
        self.assertNotIn("|", md)
        self.assertIn("- **Query**", md)
        self.assertIn("**Example:**", md)
        self.assertIn('{"a": 1}', md)

    def test_nested_table_is_flattened(self):
        md = render("<table><tr><th>Outer</th><th>Detail</th></tr><tr><td>row</td><td>"
                    "<table><tr><th>K</th><th>V</th></tr><tr><td>x</td><td>1</td></tr></table></td></tr></table>")
        self.assertNotIn("|", md)
        self.assertIn("**row**", md)
        self.assertIn("**x**", md)

    def test_image_viewport_wrapper_is_unwrapped(self):
        md = render('<div class="mediaobject"><table class="image-viewport"><tr><td>'
                    '<img src="image/a.png" alt="A real caption here"/></td></tr></table></div>')
        self.assertEqual(md, "[Image: A real caption here]")


class Links(unittest.TestCase):
    PAGES = {"34965-1-a.html": ("p-1", "main"), "34965-2-api.html": ("p-2", "api")}

    def test_internal_link_goes_to_the_anchor_and_drops_the_uuid(self):
        md = render('<a href="34965-1-a.html#UUID-abc">A</a>', pages=self.PAGES)
        self.assertEqual(md, "[A](#p-1)")

    def test_link_to_the_other_part_or_an_excluded_page_uses_the_live_site(self):
        self.assertEqual(render('<a href="34965-2-api.html">API</a>', pages=self.PAGES),
                         f"[API]({BASE}34965-2-api.html)")
        self.assertEqual(render('<a href="34965-9-gone.html#x">G</a>', pages=self.PAGES),
                         f"[G]({BASE}34965-9-gone.html)")

    def test_absolute_docs_link_is_treated_like_a_relative_one(self):
        self.assertEqual(render(f'<a href="{BASE}34965-1-a.html">A</a>', pages=self.PAGES), "[A](#p-1)")

    def test_in_page_jump_keeps_only_the_words(self):
        self.assertEqual(render('<a href="#UUID-1_body">below</a>'), "below")

    def test_external_and_mailto_untouched(self):
        self.assertEqual(render('<a href="https://example.com/x">x</a>'), "[x](https://example.com/x)")
        self.assertEqual(render('<a href="mailto:a@b.c">m</a>'), "[m](mailto:a@b.c)")


class Headings(unittest.TestCase):
    def test_depth_is_preserved_then_becomes_bold_lead_ins(self):
        body = "# Title\n\n## A\n\n### B\n\n#### C\n\n##### D\n\n###### E\n\ntext"
        out = R.demote_headings(body, 2)
        self.assertNotIn("Title", out)
        self.assertIn("#### A", out)
        self.assertIn("##### B", out)
        self.assertIn("###### C", out)
        self.assertIn("**D**", out)
        self.assertIn("**E**", out)
        self.assertEqual(out.count("######"), 1)     # only C; D and E no longer pile up at H6

    def test_code_fences_are_left_alone(self):
        out = R.demote_headings("## A\n\n```\n# not a heading\n```\n\n- ```\n  # also code\n  ```\n\n## B", 2)
        self.assertIn("# not a heading", out)
        self.assertIn("  # also code", out)
        self.assertIn("#### B", out)


class Quality(unittest.TestCase):
    def test_metrics(self):
        text = "\n".join([
            "# Doc", "## Contents", "- [Products](#products)", "## Products",
            "### One {#p-1}", "[ok](#p-1) [dead](#p-9) [stale](34965-3-x.html) [site](https://x.y/a.html)",
            "![bad](image/a.png) ![good](https://x.y/a.png)",
            "###### deep", "### Two {#p-1}", "```", "[in code](#nope) ###### no", "```"])
        m = R.quality_metrics(text, D.slugify)
        self.assertEqual((m["dead_links"], m["broken_images"], m["h6_headings"], m["duplicate_ids"]), (2, 1, 1, 1))


def rec(file, title, crumbs, nav=None, status="keep", **kw):
    r = {"file": file, "title": title, "product": crumbs[0] if crumbs else "P", "breadcrumb": crumbs,
         "modified": "", "links": [], "has_content": True, "topic_id": R.topic_id(file),
         "anchor": R.anchor_for(file), "nav": nav, "status": status, "part": "main",
         "_text_key": None, "_section": None}
    r.update(kw)
    return r


class Dedupe(unittest.TestCase):
    def test_one_record_per_topic_id_preferring_the_original_slug(self):
        a = rec("34965-253661-sca-results-253661.html", "SCA Results Viewer", ["P", "Viewing Scan Results"], nav=5)
        b = rec("34965-253661-sca-results.html", "SCA Results Viewer", ["P", "SCA Scanner"], nav=90)
        index = {a["file"]: a, b["file"]: b}
        self.assertEqual(D._collapse(index, lambda m: m["topic_id"], "duplicate-topic-id"), 1)
        self.assertEqual(b["status"], "keep")
        self.assertEqual((a["status"], a["duplicate_of"]), ("drop", b["file"]))
        self.assertEqual(b["aliases"], [a["file"]])

    def test_earlier_nav_position_breaks_ties(self):
        a = rec("34965-7-x.html", "X", ["P", "B"], nav=20)
        b = rec("34965-7-y.html", "X", ["P", "A"], nav=3)
        D._collapse({a["file"]: a, b["file"]: b}, lambda m: m["topic_id"], "dup")
        self.assertEqual((a["status"], b["status"]), ("drop", "keep"))

    def test_identical_text_is_the_second_pass(self):
        a = rec("34965-1-a.html", "A", ["P"], _text_key="same")
        b = rec("34965-2-b.html", "B", ["P"], _text_key="same")
        c = rec("34965-3-c.html", "C", ["P"], _text_key="other")
        index = {m["file"]: m for m in (a, b, c)}
        self.assertEqual(D._dedupe(index), (0, 1))
        self.assertEqual(sorted(m["status"] for m in index.values()), ["drop", "keep", "keep"])

    def test_duplicate_titles_get_the_breadcrumb_tail(self):
        a = rec("34965-1-g.html", "General Settings", ["P", "Global Account Settings"])
        b = rec("34965-2-g.html", "General Settings", ["P", "User Management"])
        c = rec("34965-3-o.html", "Other", ["P", "Else"])
        D._assign_display_titles([a, b, c])
        self.assertEqual(a["display_title"], "General Settings (Global Account Settings)")
        self.assertEqual(b["display_title"], "General Settings (User Management)")
        self.assertEqual(c["display_title"], "Other")


class Parts(unittest.TestCase):
    def test_api_reference_pages_are_tagged(self):
        a = rec("34965-1-a.html", "Endpoints", ["Checkmarx One", "Checkmarx One API Documentation"])
        b = rec("34965-2-b.html", "Scanner", ["Checkmarx One", "API Security"])
        index = {a["file"]: a, b["file"]: b}
        self.assertEqual(D._tag_parts(index, "separate", D.API_REFERENCE_CRUMBS), 1)
        self.assertEqual((a["part"], b["part"]), ("api", "main"))
        D._tag_parts(index, "inline", D.API_REFERENCE_CRUMBS)
        self.assertEqual(a["part"], "main")

    def test_empty_pages_are_dropped_before_links_resolve(self):
        a = rec("34965-1-a.html", "A", ["P"], _section=section("   "))
        b = rec("34965-2-b.html", "B", ["P"], _section=section("<p>text</p>"))
        self.assertEqual(D._drop_empty({a["file"]: a, b["file"]: b}, "strip"), 1)
        self.assertEqual((a["status"], a["drop_reason"], b["status"]), ("drop", "empty", "keep"))


class Combine(unittest.TestCase):
    def setUp(self):
        self.root = os.path.abspath(os.path.normpath(tempfile.mkdtemp()))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.extract = os.path.join(self.root, "x.json")
        self.md = os.path.join(self.root, "out.md")

    def page(self, n, title, nav, part="main", modified="", markdown="body", crumbs=("P",)):
        return {"file": f"34965-{n}-t.html", "title": title, "product": "P", "breadcrumb": list(crumbs),
                "modified": modified, "status": "keep", "markdown": markdown, "part": part,
                "anchor": f"p-{n}", "nav": nav, "topic_id": str(n), "display_title": title}

    def combine(self, pages, *flags):
        write_json(self.extract, pages)
        cfg = D.build_cfg(docs_args("--json-path", self.extract, "--md-path", self.md, *flags))
        D.stage_combine(cfg)
        return cfg, read_text(cfg.md_path)

    def test_pages_follow_navigation_order_not_alphabetical(self):
        _, text = self.combine([self.page(1, "Zebra", 0), self.page(2, "Apple", 1)])
        self.assertLess(text.index("### Zebra {#p-1}"), text.index("### Apple {#p-2}"))

    def test_headings_carry_stable_anchors(self):
        _, text = self.combine([self.page(253661, "SCA Results", 0)])
        self.assertIn("### SCA Results {#p-253661}", text)
        self.assertIn("- [SCA Results](#p-253661)", text)

    def test_api_pages_go_to_their_own_file(self):
        cfg, text = self.combine([self.page(1, "Guide", 0), self.page(2, "Endpoint", 1, part="api")])
        api_text = read_text(D._part_path(cfg.md_path, "api-reference"))
        self.assertNotIn("### Endpoint", text)
        self.assertIn("### Endpoint {#p-2}", api_text)
        self.assertIn("1 API-reference pages are in", text)

    def test_stale_index(self):
        now = datetime.now()
        old = now.replace(year=now.year - 4).strftime("%B %d, %Y")
        _, text = self.combine([self.page(1, "Old", 0, modified=old),
                                self.page(2, "New", 1, modified=now.strftime("%B %d, %Y"))])
        stale_block = text.split("**Stale pages**")[1].split("\n## ")[0]
        self.assertIn("- [Old](#p-1)", stale_block)
        self.assertNotIn("[New]", stale_block)

    def test_quality_gate_fails_after_writing_and_can_be_skipped(self):
        bad = self.page(1, "Bad", 0, markdown="[dead](#p-404)")
        with self.assertRaises(SystemExit) as cm:
            self.combine([bad])
        self.assertEqual(cm.exception.code, 3)
        self.assertTrue([f for f in os.listdir(self.root) if f.endswith(".md")])   # written anyway
        self.assertTrue(os.path.exists(self.extract))         # kept for inspection
        self.combine([bad], "--no-quality-gate")              # no exit


if __name__ == "__main__":
    unittest.main()
