"""
cx_docs_render.py — deterministic HTML -> Markdown rendering for the docs
pipeline (used by the extract and combine stages of cx_docs_mirror.py).

Everything here is pure and repeatable: the same cached HTML always gives the
same Markdown. It exists to fix defects that a generic HTML-to-Markdown
converter leaves behind:

  - admonitions (Note / Tip / Warning ...) become blockquotes, not stray headings;
    expandable (accordion) sections become a bold title plus ordinary content, and
    "Expand source" toggle text is dropped
  - nested tables and tables with block content (lists, code, several
    paragraphs) become definition-list style sections instead of mangled pipes
  - images are stripped (keeping meaningful alt text) or made absolute; a
    relative path to a file we never mirrored is never emitted
  - internal links point at stable per-page anchors, or at the live site
  - source headings deeper than H6 become bold lead-ins instead of collapsing
  - quality metrics (dead links, H6s, broken images, duplicate anchors)
"""

from __future__ import annotations

import copy
import re
from collections import Counter
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify as md

_SOUP = BeautifulSoup("", "lxml")


def _tag(name: str, **attrs) -> Tag:
    return _SOUP.new_tag(name, **attrs)


# ---------------------------------------------------------------------------
# Topic ids and anchors
# ---------------------------------------------------------------------------

_TOPIC_RE = re.compile(r"^(\d+)-(\d+)-")


def topic_id(file: str) -> str | None:
    """The Paligo topic id in a slug like `34965-253661-sca-results.html`."""
    m = _TOPIC_RE.match(Path(file).name)
    return m.group(2) if m else None


def anchor_for(file: str) -> str:
    """A stable anchor keyed on the topic id, so links survive title changes."""
    tid = topic_id(file)
    if tid:
        return f"p-{tid}"
    return "p-" + (re.sub(r"[^a-z0-9]+", "-", Path(file).stem.lower()).strip("-") or "page")


def is_reused_slug(file: str) -> bool:
    """`...-sca-results-253661.html`: the same topic re-published under another parent."""
    tid = topic_id(file)
    return bool(tid) and Path(file).stem.endswith(f"-{tid}")


# ---------------------------------------------------------------------------
# Rendering context
# ---------------------------------------------------------------------------

class RenderCtx:
    """What the renderer needs to know about the rest of the document set."""

    def __init__(self, base_url: str, images: str = "strip", resolve=None):
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.host = urlparse(base_url).netloc
        self.images = images                  # "strip" | "absolute"
        self.resolve = resolve or (lambda name: None)   # file name -> {"anchor", "part"} | None
        self.stats: Counter = Counter()
        self.part = "main"


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

_IMG_EXT = re.compile(r"\.(png|jpe?g|gif|webp|svg|bmp)$", re.I)


def meaningful_alt(alt: str | None) -> bool:
    """Alt text a person wrote, as opposed to a file name or id."""
    a = (alt or "").strip()
    if not a or _IMG_EXT.search(a):
        return False
    if re.fullmatch(r"[\w\-.]+", a):          # one token such as Custom_Fields_Migrated_as_Tags_
        return False
    return len(re.findall(r"[A-Za-z]{2,}", a)) >= 2


def _images(sec: Tag, ctx: RenderCtx) -> None:
    for img in list(sec.find_all("img")):
        alt = (img.get("alt") or "").strip()
        if ctx.images == "absolute":
            src = (img.get("src") or "").strip()
            if not src:
                img.decompose()
                continue
            img["src"] = urljoin(ctx.base_url, src)
            img["alt"] = alt if meaningful_alt(alt) else ""
            img.attrs.pop("style", None)
            ctx.stats["images_absolute"] += 1
        elif meaningful_alt(alt):
            img.replace_with(NavigableString(f"[Image: {alt}]"))
            ctx.stats["images_kept_as_text"] += 1
        else:
            img.decompose()
            ctx.stats["images_removed"] += 1


# ---------------------------------------------------------------------------
# Admonitions and sidebars -> blockquotes
# ---------------------------------------------------------------------------

ADMONITIONS = ("note", "notice", "tip", "warning", "caution", "important", "danger", "attention")
_HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]


def _blockquote(div: Tag, label: str) -> Tag:
    label = " ".join(label.split())
    label = label if label.endswith((":", "?")) else label + ":"
    bq = _tag("blockquote")
    lead = _tag("strong")
    lead.string = label
    first = next((c for c in div.children if isinstance(c, Tag)), None)
    if first is not None and first.name == "p":
        first.insert(0, NavigableString(" "))
        first.insert(0, lead)
    else:
        p = _tag("p")
        p.append(lead)
        bq.append(p)
    for child in list(div.contents):
        bq.append(child.extract())
    return bq


def _accordion(div: Tag) -> None:
    """An expandable section is content, not a callout: its title becomes a bold
    lead-in and its body is unwrapped in place, so it is not buried in a blockquote."""
    title = div.find(class_="sidebar-title")
    body = div.find(class_="panel-body")
    label = " ".join(title.get_text(" ", strip=True).split()) if title else ""
    nodes: list = []
    if label:
        lead = _tag("p")
        strong = _tag("strong")
        strong.string = label
        lead.append(strong)
        nodes.append(lead)
    if body is not None:
        nodes += list(body.contents)
    for node in nodes:
        div.insert_before(node.extract() if node.parent is not None else node)
    div.decompose()


# Text of the show/hide controls that the site's scripts normally turn into buttons.
TOGGLE_TEXT = ("expand source", "collapse source")


def _toggles(sec: Tag, ctx: RenderCtx) -> None:
    for p in sec.find_all("p"):
        if p.get_text(strip=True).lower() in TOGGLE_TEXT:
            p.decompose()
            ctx.stats["toggles_removed"] += 1


def _admonitions(sec: Tag, ctx: RenderCtx) -> None:
    for div in reversed(sec.find_all("div")):                 # innermost first
        classes = div.get("class") or []
        kind = next((c for c in classes if c in ADMONITIONS), None)
        if kind:
            title = div.find(_HEADINGS, class_="title", recursive=False)
            label = title.get_text(" ", strip=True) if title else ""
            if title:
                title.decompose()
            div.replace_with(_blockquote(div, label or kind.capitalize()))
            ctx.stats["callouts"] += 1
        elif "sidebar" in classes and "accordion" in classes:
            _accordion(div)
            ctx.stats["accordions"] += 1
        elif "sidebar" in classes:
            title = div.find(class_="sidebar-title")
            if title is None:
                continue
            label = title.get_text(" ", strip=True)
            wrapper = title.find_parent("div", class_="titlepage")
            (wrapper if wrapper is not None and div in wrapper.parents else title).decompose()
            div.replace_with(_blockquote(div, label or "Sidebar"))
            ctx.stats["callouts"] += 1


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------

_BLOCK = ["table", "ul", "ol", "pre", "blockquote", "dl"] + _HEADINGS


def _own_rows(table: Tag) -> list[Tag]:
    return [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]


def _cells(tr: Tag) -> list[Tag]:
    return tr.find_all(["td", "th"], recursive=False)


def _complex_cell(cell: Tag) -> bool:
    """A pipe table cannot hold lists, code, nested tables or several paragraphs."""
    return cell.find(_BLOCK) is not None or len(cell.find_all("p")) > 1


def _text1(el: Tag) -> str:
    return " ".join(el.get_text(" ", strip=True).split())


def _div_of(cells: list[Tag]) -> Tag:
    div = _tag("div")
    for c in cells:
        for child in list(c.contents):
            div.append(child.extract())
    return div


def _as_list(table: Tag, rows: list[Tag]) -> Tag:
    """Definition-list style: one bullet per row, one sub-bullet per column."""
    first = rows[0]
    has_header = bool(first.find_all("th", recursive=False)) and not first.find_all("td", recursive=False)
    has_header = has_header or first.find_parent("thead") is not None
    headers = [_text1(c) for c in _cells(first)] if has_header else []
    data = rows[1:] if has_header else rows
    ul = _tag("ul")
    for n, tr in enumerate(data, 1):
        cells = _cells(tr)
        if not cells:
            continue
        li = _tag("li")
        if len(cells) == 1:
            for child in list(cells[0].contents):
                li.append(child.extract())
            ul.append(li)
            continue
        key_cell = cells[0]
        if not _complex_cell(key_cell) and 0 < len(_text1(key_cell)) <= 120:
            key, rest, offset = _text1(key_cell), cells[1:], 1
        else:
            key, rest, offset = f"Row {n}", cells, 0
        strong = _tag("strong")
        strong.string = key
        li.append(strong)
        inner = _tag("ul")
        for i, cell in enumerate(rest):
            if not cell.get_text(strip=True) and not cell.find(["img", "pre", "table", "ul", "ol"]):
                continue
            item = _tag("li")
            head = headers[offset + i] if offset + i < len(headers) else ""
            if head:
                label = _tag("strong")
                label.string = head + ":"
                item.append(label)
                item.append(NavigableString(" "))
            for child in list(cell.contents):
                item.append(child.extract())
            inner.append(item)
        li.append(inner)
        ul.append(li)
    return ul


def _tables(sec: Tag, ctx: RenderCtx) -> None:
    for t in sec.find_all("table", class_="image-viewport"):       # image wrappers, not data
        t.replace_with(_div_of([c for r in _own_rows(t) for c in _cells(r)]))
    for table in reversed(sec.find_all("table")):                  # innermost first
        if table.parent is None:
            continue
        rows = _own_rows(table)
        cells = [c for r in rows for c in _cells(r)]
        nested = table.find_parent("table") is not None            # a table inside a cell is always flattened
        if not cells or not (nested or any(_complex_cell(c) for c in cells)):
            continue
        if len(rows) == 1 and len(cells) == 1:                     # a layout table: keep the content
            table.replace_with(_div_of(cells))
            ctx.stats["tables_unwrapped"] += 1
        else:
            table.replace_with(_as_list(table, rows))
            ctx.stats["tables_as_lists"] += 1


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

def _links(sec: Tag, ctx: RenderCtx) -> None:
    for a in list(sec.find_all("a")):
        href = (a.get("href") or "").strip()
        if not href:
            a.unwrap()
            continue
        if not a.get_text(strip=True) and not a.find(["img", "code"]):
            a.decompose()                                          # was only an image we removed
            continue
        if href.startswith("#"):                                   # in-page jump: keep the words only
            a.unwrap()
            continue
        u = urlparse(href)
        if u.scheme in ("mailto", "tel"):
            continue
        if u.scheme in ("http", "https") and u.netloc != ctx.host:
            continue
        name = Path(unquote(u.path)).name
        if not name.endswith(".html"):
            if not u.scheme:                                       # relative non-page link: make it absolute
                a["href"] = urljoin(ctx.base_url, href)
            continue
        target = ctx.resolve(name)
        if target is not None and target["part"] == ctx.part:
            a["href"] = "#" + target["anchor"]                     # fragment dropped: UUIDs have no heading
            ctx.stats["links_internal"] += 1
        else:
            a["href"] = urljoin(ctx.base_url, name)               # the live page
            ctx.stats["links_live_site"] += 1


def is_empty(section: Tag, images: str = "strip") -> bool:
    """A topic with no words beyond its own title (and, in absolute mode, no images)
    renders to nothing, because the page title is dropped from the body."""
    h1 = section.find("h1")
    title = h1.get_text(strip=True) if h1 else ""
    if section.get_text(strip=True).replace(title, "", 1).strip():
        return False
    return not (images == "absolute" and section.find("img"))


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def prepare(section: Tag, ctx: RenderCtx) -> Tag:
    """A cleaned copy of a topic <section>, ready for Markdown conversion."""
    sec = copy.copy(section)
    for el in sec.find_all("a", class_="header-link"):
        el.decompose()
    for el in sec.find_all("i", class_="fa"):
        el.decompose()
    for el in sec.find_all(["script", "style"]):
        el.decompose()
    _images(sec, ctx)
    _toggles(sec, ctx)
    _admonitions(sec, ctx)
    _tables(sec, ctx)
    _links(sec, ctx)
    return sec


def to_markdown(section: Tag, ctx: RenderCtx) -> str:
    body = md(str(prepare(section, ctx)), heading_style="ATX", bullets="-").strip()
    return re.sub(r"\n{3,}", "\n\n", body)


_ATX = re.compile(r"^(#{1,6})(\s.*)$")
# a code fence, possibly opened on a list-item line (`- ```)
_FENCE = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+)?```")


def demote_headings(body: str, by: int = 2, stats: Counter | None = None) -> str:
    """Shift ATX headings `by` levels deeper so a page nests under its H3, drop
    the body's leading H1 (the page title), and turn anything that would pass
    H6 into a bold lead-in instead of clamping it to H6. Code fences are left alone."""
    out: list[str] = []
    in_code = dropped_h1 = False
    for line in body.split("\n"):
        if _FENCE.match(line):
            in_code = not in_code
            out.append(line)
            continue
        m = None if in_code else _ATX.match(line)
        if not m:
            out.append(line)
            continue
        level, text = len(m.group(1)), m.group(2).strip()
        if not dropped_h1 and level == 1:
            dropped_h1 = True
            continue
        new = level + by
        if new > 6:
            if stats is not None:
                stats["headings_to_lead_ins"] += 1
            if text:
                out += ["", text if text.startswith("**") else f"**{text}**", ""]
            continue
        out.append("#" * new + " " + text)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# ---------------------------------------------------------------------------
# Quality metrics
# ---------------------------------------------------------------------------

_LINK = re.compile(r"(!?)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_ANCHOR = re.compile(r"\{#([^}\s]+)\}")


def quality_metrics(text: str, heading_slug) -> dict:
    """Counts that turn the known defects into regression checks.

    dead_links     in-document `#anchor` links with no target, and relative `.html` links
    broken_images  image references that are not absolute URLs
    h6_headings    lines at the deepest ATX level
    duplicate_ids  `{#p-...}` anchors defined more than once
    blockquote_pct share of the document that sits on `>` lines (report only)
    toggle_leftovers  "Expand source" style control text that should have been dropped"""
    lines, in_code, body = text.split("\n"), False, []
    for ln in lines:
        if _FENCE.match(ln):
            in_code = not in_code
            continue
        if not in_code:
            body.append(ln)
    joined = "\n".join(body)
    anchors = _ANCHOR.findall("\n".join(ln for ln in body if ln.startswith("#")))
    known = set(anchors) | {heading_slug(ln.lstrip("#").strip()) for ln in body if ln.startswith("## ")}
    dead, broken = [], 0
    for bang, target in _LINK.findall(joined):
        if bang:
            if not target.startswith(("http://", "https://", "data:")):
                broken += 1
        elif target.startswith("#"):
            if target[1:] not in known:
                dead.append(target)
        elif not urlparse(target).scheme and target.split("#")[0].endswith(".html"):
            dead.append(target)
    counts = Counter(anchors)
    total = len(text.encode("utf-8")) or 1
    quoted = sum(len(ln.encode("utf-8")) + 1 for ln in body if ln.startswith(">"))
    return {
        "blockquote_pct": round(100 * quoted / total, 1),
        "toggle_leftovers": sum(1 for ln in body if ln.strip().lower().lstrip("> ") in TOGGLE_TEXT),
        "dead_links": len(dead), "dead_link_examples": sorted(set(dead))[:5],
        "broken_images": broken,
        "h6_headings": sum(1 for ln in body if ln.startswith("###### ")),
        "duplicate_ids": sum(n - 1 for n in counts.values() if n > 1),
        "pages": len(anchors),
    }
