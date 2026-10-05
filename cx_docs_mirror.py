#!/usr/bin/env python3
"""
cx_docs_mirror.py — Checkmarx documentation downloader. Two independent jobs
that write to separate outputs:

  USER DOCS  (docs.checkmarx.com -> one Markdown file, in four stages)
    1. MIRROR    Read the live nav, fetch allowed pages over plain HTTP, and
                 write only pages whose topic content changed. Produces a
                 manifest, a state file, and a changes-<date>.md report.
    2. EXTRACT   Filter by product, rescue cross-linked exceptions, convert the
                 real content of each kept page to faithful Markdown.
    3. COMBINE   Merge everything into ONE canonical .md with a clean heading
                 hierarchy (doc > product > page > page-content).
    4. COMPRESS  Post-process an existing canonical .md, dropping version /
                 changelog pages and duplicates. Not part of the default run.

  API SPEC   (live tenant catalog + Stoplight -> one OpenAPI 3.0.3 candidate)
    api-spec     Fetch both sources, merge them, and report drift against a
                 baseline spec. Everything lands in ./api-spec/ and never mixes
                 with the user-docs files. See cx_api_spec.py for the details.

Run everything (default), or any part in isolation:

    python cx_docs_mirror.py                 # docs stages + api-spec
    python cx_docs_mirror.py --stage docs    # mirror -> extract -> combine only
    python cx_docs_mirror.py --stage mirror
    python cx_docs_mirror.py --stage extract
    python cx_docs_mirror.py --stage combine
    python cx_docs_mirror.py --stage compress
    python cx_docs_mirror.py --stage api-spec --baseline path/to/cxone_openapi.json

All knobs live in the CONFIG block below (target URL, what to include/exclude,
crawl tuning, output paths, product ordering). Anything there can also be
overridden on the command line — see `--help`. The api-spec stage has its own
defaults at the top of cx_api_spec.py.

------------------------------------------------------------------------------
SETUP (Python 3.9+):
    python -m pip install -r requirements.txt
    (needs: httpx, beautifulsoup4, lxml, markdownify, PyYAML. Playwright is no
    longer used; the docs site serves topic content as static HTML.)

Use --force to refetch and rewrite every page regardless of cache.
------------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup
from markdownify import markdownify as md

# ===========================================================================
# CONFIG  — edit these defaults; all are overridable via command-line flags.
# ===========================================================================

# Where the crawl starts. This is the page whose directory bounds the crawl.
START_URL = "https://docs.checkmarx.com/en/34965-68517-checkmarx-one-user-guide.html"

# --- What to keep -----------------------------------------------------------
# INCLUDE_PRODUCTS is the primary crawl-time allowlist. The mirror stage uses
# the docs navigation tree to seed only these product branches, then verifies
# each downloaded page's breadcrumb root against this set and prunes anything
# that slipped through. Leave empty to crawl everything (subject to
# EXCLUDE_PRODUCTS). The extract stage uses the same list for its own filter.
INCLUDE_PRODUCTS: list[str] = [
    "Checkmarx One",
    "Checkmarx Developer Assist",
    "Checkmarx DAST",
    "Checkmarx Codebashing",
    "Malicious Package Identification API (MPIAPI)",
    "Checkmarx CheckAI",
    "Checkmarx Idea Portal",
]
# EXCLUDE_PRODUCTS is only consulted when INCLUDE_PRODUCTS is empty.
EXCLUDE_PRODUCTS: list[str] = []

# Cross-product page rescue in the extract stage. Kept at 0: the mirror stage
# now prunes excluded branches at crawl time, so rescuing cross-links would
# reintroduce content we deliberately skipped.
RESCUE_DEPTH = 0

# A page must have at least this many characters of real content to count as
# healthy; below this it's flagged 'thin'.
MIN_CHARS = 200

# Drop pages whose TITLE matches any of these (case-insensitive regex). Targets
# low-value version-history / changelog noise that bloats a knowledge base
# without describing how the product works. Applied in both `extract` (fresh
# runs) and `compress` (post-processing an existing canonical doc).
TITLE_EXCLUDE_PATTERNS: list[str] = [
    r"release notes",
    r"releases of",
    r"\bchange ?log\b",
    r"\bversion \d",
    r"single[- ]tenant version",
    r"multi[- ]tenant",
    r"engine pack",
    r"\bhotfix",
    r"content pack",
    r"\brs[- ]\d",
]

# --- Crawl tuning -----------------------------------------------------------
CONCURRENCY = 8            # simultaneous HTTP requests
MAX_DEPTH = 3              # unused since the nav-driven mirror; kept for CLI compatibility
NAV_TIMEOUT_MS = 45000     # per-request HTTP timeout (ms)
RETRIES = 3
POLITE_WAIT = 0.3          # per-worker delay between pages (seconds)

# --- Output paths -----------------------------------------------------------
HTML_DIR = "cx-docs"                       # mirror output (pages land in <HTML_DIR>/en/)
EXTRACT_JSON = "cx-extracted.json"         # intermediate: records + bodies + audit
CANONICAL_MD = "checkmarx-one-docs.md"     # final single-file deliverable

# --- Canonical document framing --------------------------------------------
DOC_TITLE = "Checkmarx One — Documentation (Filtered Mirror)"
# Products are emitted in this order; any not listed are appended afterwards.
PRODUCT_ORDER = [
    "Checkmarx One",
    "Checkmarx Developer Assist",
    "Checkmarx DAST",
    "Checkmarx Codebashing",
    "Malicious Package Identification API (MPIAPI)",
    "Checkmarx CheckAI",
    "Checkmarx Idea Portal",
]

# Asset extensions never followed or saved (we keep rendered HTML only).
SKIP_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".woff", ".woff2", ".ttf", ".eot",
    ".js", ".css", ".json", ".xml", ".pdf", ".zip", ".gz",
}

# A mirrored page is worth keeping in these two states; anything else is a
# navigation shell or an error page.
USABLE_STATUSES = ("ok", "thin")


# ===========================================================================
# Shared helpers
# ===========================================================================

def is_in_scope(link: str, root: str) -> bool:
    """Same-host and at/below the root path (the wget -np 'no parent' rule)."""
    root_p, link_p = urlparse(root), urlparse(link)
    if link_p.scheme not in ("http", "https"):
        return False
    if link_p.netloc != root_p.netloc:
        return False
    root_dir = root_p.path.rsplit("/", 1)[0] + "/"
    if not link_p.path.startswith(root_dir):
        return False
    if Path(link_p.path).suffix.lower() in SKIP_EXTENSIONS:
        return False
    return True


def url_to_path(url: str, out_dir: Path) -> Path:
    path = urlparse(url).path
    if path.endswith("/") or path == "":
        path += "index.html"
    elif not Path(path).suffix:
        path += ".html"
    return out_dir / path.lstrip("/")


def parse_page(html: str, base_url: str, root: str,
               min_chars: int) -> tuple[str, str | None, set[str]]:
    """One BeautifulSoup pass → (status, product_root, in_scope_links).

    status:       'ok' | 'thin' | 'shell'
    product_root: first breadcrumb item, or None when there is no breadcrumb."""
    soup = BeautifulSoup(html, "lxml")

    # Breadcrumb root — first non-current-category <li> text.
    product_root: str | None = None
    ul = soup.find("ul", class_="breadcrumb")
    if ul:
        for li in ul.find_all("li", recursive=False):
            if li.find("span", class_="current-category"):
                continue
            t = li.get_text(strip=True)
            if t:
                product_root = t
                break

    tc = soup.find(id="topic-content")
    section = tc.find("section") if tc else None
    if section is None:
        status = "shell"
    else:
        n_chars = len(section.get_text(" ", strip=True))
        status = "ok" if n_chars >= min_chars else "thin"

    links = set()
    for a in soup.find_all("a", href=True):
        absolute, _ = urldefrag(urljoin(base_url, a["href"]))
        if is_in_scope(absolute, root):
            links.add(absolute)
    return status, product_root, links


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_-]+", "-", s) or "section"


def digest(text: str) -> str:
    """Stable content fingerprint for dedup.

    Uses sha256 rather than hash() so the same body produces the same key in
    every process, regardless of PYTHONHASHSEED."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _dated_path(path: str) -> str:
    """Insert today's date into a file stem: foo.md → foo-2026-08-27.md."""
    p = Path(os.path.normpath(path))
    return str(p.with_stem(f"{p.stem}-{time.strftime('%Y-%m-%d')}"))


def compile_title_filters(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.I) for p in patterns]


def title_excluded(title: str, filters: list[re.Pattern]) -> bool:
    return any(rx.search(title) for rx in filters)


def order_products(products, product_order: list[str]) -> list[str]:
    """Configured order first, then anything unlisted, alphabetically."""
    listed = [p for p in product_order if p in products]
    return listed + sorted(p for p in products if p not in product_order)


def contents_lines(ordered: list[str], counts: dict[str, int],
                   n_referenced: int) -> list[str]:
    """Top-level table of contents (products only — page lists live in each
    section). Shared by the combine and compress renderers."""
    lines = ["## Contents\n"]
    for p in ordered:
        lines.append(f"- [{p}](#{slugify(p)}) ({counts[p]} pages)")
    if n_referenced:
        lines.append(f"- [Referenced Material](#referenced-material) "
                     f"({n_referenced} pages)")
    lines.append("")
    return lines


# ===========================================================================
# STAGE 1 — MIRROR (live nav → conditional HTTP fetch → change detection)
# ===========================================================================
#
# How a run works:
#   1. GET the start page once and read the full sidebar nav. Every page on
#      the site embeds the whole tree, so this one request lists every live
#      URL and the top-level product each one sits under. Only URLs under
#      INCLUDE_PRODUCTS are fetched; excluded products are never requested.
#   2. For each URL, send a plain HTTP GET (browser headers, no Playwright).
#      If we have a cached copy and a stored Last-Modified, it's sent as
#      If-Modified-Since; a 304 means the page is unchanged.
#   3. On a 200, the topic body (#topic-content section) is hashed and compared
#      with the cached copy. Only real content changes are written to disk.
#      The sidebar is ignored for comparison, since it changes on every page
#      whenever anything is added to the site.
#   4. Write manifest.json (the files that are live this run) so the extract
#      stage ignores stale cached slugs, plus mirror-state.json (hashes,
#      dates, titles) and a changes-<date>.md report of new/updated/removed.

STATE_FILE = "mirror-state.json"
MANIFEST_FILE = "manifest.json"

HTTP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def stage_mirror(cfg: SimpleNamespace) -> None:
    asyncio.run(_mirror(cfg))


def _topic_info(html: str) -> tuple[str | None, str, str, str]:
    """→ (body_hash or None if no topic section, title, topic_modified, breadcrumb root)."""
    soup = BeautifulSoup(html, "lxml")
    tc = soup.find(id="topic-content")
    section = tc.find("section") if tc else None
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""
    crumbs = _breadcrumb(soup)
    if section is None:
        return None, title, "", crumbs[0] if crumbs else ""
    text = section.get_text(" ", strip=True)
    return (digest(text), title, section.get("data-time-modified", ""),
            crumbs[0] if crumbs else "")


def _nav_root(soup):
    """Same selector cascade as before: aside/ul.toc before <nav>, because on
    this site <nav> is the header bar, not the product tree."""
    for sel_type, sel_val in [
        ("tag", "aside"), ("class", "nav-site-sidebar"), ("class", "toc"),
        ("class", "sidenav"), ("class", "navigation"), ("id", "navigation"),
        ("class", "sidebar"), ("id", "sidebar"), ("tag", "nav"),
    ]:
        if sel_type == "tag":
            el = soup.find(sel_val)
        elif sel_type == "class":
            el = soup.find(class_=sel_val)
        else:
            el = soup.find(id=sel_val)
        if el:
            return el
    return None


def _discover_from_nav(html: str, cfg: SimpleNamespace) -> tuple[dict[str, str], set[str]]:
    """Parse the sidebar → ({url: product} for allowed products, all product names seen).

    A link's product is the first link of its OUTERMOST <li> inside the nav
    tree, i.e. the top-level product node it hangs under."""
    soup = BeautifulSoup(html, "lxml")
    root = _nav_root(soup)
    if root is None:
        return {}, set()
    allowed = {p.strip().lower() for p in cfg.include}
    urls: dict[str, str] = {}
    seen_products: set[str] = set()
    for a in root.find_all("a", href=True):
        outer = None
        for parent in a.parents:
            if parent is root:
                break
            if parent.name == "li":
                outer = parent
        if outer is None:
            continue
        head = outer.find("a")
        product = head.get_text(strip=True) if head else ""
        if not product:
            continue
        seen_products.add(product)
        if allowed and product.lower() not in allowed:
            continue
        url, _ = urldefrag(urljoin(cfg.url, a["href"]))
        if is_in_scope(url, cfg.url):
            urls.setdefault(url, product)
    return urls, seen_products


async def _http_get(client, url: str, cfg: SimpleNamespace, headers: dict):
    """GET with retries and backoff on network errors, 429, 403 and 5xx."""
    last_exc = None
    for attempt in range(cfg.retries):
        try:
            r = await client.get(url, headers=headers)
            if r.status_code in (200, 304, 404, 410):
                return r
            last_exc = f"HTTP {r.status_code}"
        except Exception as e:                       # httpx.TransportError etc.
            last_exc = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
        await asyncio.sleep(2 * (attempt + 1))
    print(f"      giving up on {url}: {last_exc}", file=sys.stderr)
    return None


async def _process(url: str, product: str, client, cfg, out_dir: Path, state: dict,
                   sem: asyncio.Semaphore, stats: dict, changes: dict, failures: list,
                   manifest: list):
    async with sem:
        dest = url_to_path(url, out_dir)
        rel = dest.relative_to(out_dir).as_posix()
        prev = state.get(url, {})
        cached = dest.exists() and not cfg.force

        headers = {}
        if cached and prev.get("last_modified"):
            headers["If-Modified-Since"] = prev["last_modified"]

        r = await _http_get(client, url, cfg, headers)
        await asyncio.sleep(cfg.wait)

        if r is None or r.status_code in (404, 410):
            stats["failed"] += 1
            failures.append(url)
            print(f"  ! FAILED ({'no response' if r is None else r.status_code}): {url}",
                  file=sys.stderr)
            if cached:                               # keep last good copy in the doc
                manifest.append(rel)
            return

        if r.status_code == 304:
            stats["not_modified"] += 1
            manifest.append(rel)
            print(f"[304]       {url}")
            return

        html = r.text
        body_hash, title, topic_mod, crumb = await asyncio.to_thread(_topic_info, html)
        if body_hash is None:
            stats["failed"] += 1
            failures.append(url)
            print(f"  ! FAILED (no topic content): {url}", file=sys.stderr)
            if cached:
                manifest.append(rel)
            return

        old_hash = prev.get("hash")
        if cached and old_hash is None:              # first run on this cache: hash the old copy
            old_html = await asyncio.to_thread(
                dest.read_text, encoding="utf-8", errors="replace")
            old_hash = (await asyncio.to_thread(_topic_info, old_html))[0]

        entry = {"file": rel, "title": title, "product": product, "breadcrumb_root": crumb,
                 "topic_modified": topic_mod,
                 "last_modified": r.headers.get("last-modified", ""),
                 "hash": body_hash, "checked": time.strftime("%Y-%m-%d")}

        if cached and old_hash == body_hash:
            stats["unchanged"] += 1
            print(f"[same]      {url}")
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(dest.write_text, html, encoding="utf-8")
            kind = "updated" if cached else "new"
            stats[kind] += 1
            changes[kind].append(entry | {"url": url, "prev_modified": prev.get("topic_modified", "")})
            print(f"[{kind:<9}] {url}")

        state[url] = entry
        manifest.append(rel)


def _write_change_report(path: Path, changes: dict, removed: list[dict]) -> None:
    date = time.strftime("%Y-%m-%d")
    lines = [f"# Checkmarx docs changes — {date}\n",
             f"New: {len(changes['new'])} · Updated: {len(changes['updated'])} · "
             f"Removed from nav: {len(removed)}\n"]

    def block(heading, rows, show_prev=False):
        if not rows:
            return
        lines.append(f"## {heading}\n")
        for e in sorted(rows, key=lambda e: (e.get("product", ""), e.get("title", ""))):
            mod = e.get("topic_modified") or "?"
            if show_prev and e.get("prev_modified"):
                mod = f"{e['prev_modified']} → {mod}"
            lines.append(f"- **{e.get('title') or e.get('file')}** ({e.get('product', '')}) "
                         f"· {mod} · `{e.get('file')}`")
        lines.append("")

    block("New pages", changes["new"])
    block("Updated pages", changes["updated"], show_prev=True)
    block("Removed from nav (renamed or unpublished)", removed)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _mirror(cfg: SimpleNamespace) -> None:
    import httpx

    out_dir = Path(cfg.html_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir.parent
    state_path, manifest_path = base / STATE_FILE, base / MANIFEST_FILE
    fail_log = base / "failures.log"
    state: dict = (json.loads(state_path.read_text(encoding="utf-8"))
                   if state_path.exists() else {})

    start = time.time()
    limits = httpx.Limits(max_connections=cfg.concurrency)
    async with httpx.AsyncClient(headers=HTTP_HEADERS, http2=False, limits=limits,
                                 timeout=cfg.timeout / 1000, follow_redirects=True) as client:
        r = await _http_get(client, cfg.url, cfg, {})
        if r is None or r.status_code != 200:
            sys.exit(f"[mirror] could not load start page {cfg.url} "
                     f"({'no response' if r is None else r.status_code})")
        urls, products_seen = _discover_from_nav(r.text, cfg)
        if not urls:
            sys.exit("[mirror] nav parse found no pages for INCLUDE_PRODUCTS. "
                     f"Top-level products seen: {sorted(products_seen) or 'none'}")

        per_product: dict[str, int] = {}
        for p in urls.values():
            per_product[p] = per_product.get(p, 0) + 1
        print(f"[mirror] {len(urls)} live URLs from nav across {len(per_product)} products:")
        for p, n in sorted(per_product.items(), key=lambda kv: -kv[1]):
            print(f"         {n:>4}  {p}")
        missing = {p for p in cfg.include} - set(per_product)
        if missing:
            print(f"[mirror] WARNING: no nav entries for {sorted(missing)}", file=sys.stderr)

        items = list(urls.items())
        if cfg.max_pages:
            items = items[:cfg.max_pages]

        stats = {"new": 0, "updated": 0, "unchanged": 0, "not_modified": 0, "failed": 0}
        changes = {"new": [], "updated": []}
        failures: list[str] = []
        manifest: list[str] = []
        sem = asyncio.Semaphore(cfg.concurrency)
        await asyncio.gather(*[
            _process(u, p, client, cfg, out_dir, state, sem, stats, changes, failures, manifest)
            for u, p in items])

    # Pages we tracked before that are no longer in the nav (skip when --max-pages
    # truncated the run, since then absence proves nothing).
    removed = []
    if not cfg.max_pages:
        live = set(urls)
        for u in [u for u in state if u not in live]:
            removed.append(state.pop(u) | {"url": u})

    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    manifest_path.write_text(json.dumps(sorted(set(manifest)), indent=2), encoding="utf-8")
    report = base / f"changes-{time.strftime('%Y-%m-%d')}.md"
    _write_change_report(report, changes, removed)
    if failures:
        fail_log.write_text("\n".join(failures) + "\n", encoding="utf-8")

    stale = [p for p in out_dir.rglob("*.html")
             if p.relative_to(out_dir).as_posix() not in set(manifest)]
    dt = time.time() - start
    print(f"\n[mirror] done in {dt/60:.1f} min — new {stats['new']}, updated {stats['updated']}, "
          f"unchanged {stats['unchanged'] + stats['not_modified']} "
          f"(304: {stats['not_modified']}), failed {stats['failed']}")
    print(f"[mirror] removed from nav since last run: {len(removed)}; "
          f"stale cached files ignored by extract: {len(stale)}")
    print(f"[mirror] wrote {manifest_path.name}, {state_path.name}, {report.name}")
    if stats["new"] + stats["updated"] == 0:
        print("[mirror] no content changes detected this run.")


# ===========================================================================
# STAGE 2 — EXTRACT (filter + rescue + convert to markdown)
# ===========================================================================

def _breadcrumb(soup) -> list[str]:
    ul = soup.find("ul", class_="breadcrumb")
    if not ul:
        return []
    out = []
    for li in ul.find_all("li", recursive=False):
        if li.find("span", class_="current-category"):
            continue
        out.append(li.get_text(strip=True))
    return out


def _clean_body_md(section) -> str:
    # Strip non-content cruft before conversion.
    for el in section.find_all("a", class_="header-link"):
        el.decompose()
    for el in section.find_all("i", class_="fa"):
        el.decompose()
    for el in section.find_all(["script", "style"]):
        el.decompose()
    body = md(str(section), heading_style="ATX", bullets="-").strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    # Flatten useless in-page anchor links: [text](#frag) -> text
    body = re.sub(r"\[([^\]]+)\]\(#[^)]*\)", r"\1", body)
    return body


def _index_pages(files: list[Path]) -> dict[str, dict]:
    """Parse every mirrored page into a metadata record keyed by file name.

    The page's BeautifulSoup <section> rides along under '_section' for the
    conversion pass; underscore keys are stripped before serialization."""
    index: dict[str, dict] = {}
    for path in files:
        html = path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        tc = soup.find(id="topic-content")
        section = tc.find("section") if tc else None
        crumbs = _breadcrumb(soup)
        h1 = soup.find("h1")
        links = []
        if section:
            for a in section.find_all("a", href=True):
                href = a["href"].split("#")[0].strip()
                if href.endswith(".html"):
                    links.append(Path(href).name)
        index[path.name] = {
            "file": path.name,
            "title": h1.get_text(strip=True) if h1 else path.stem,
            "product": crumbs[0] if crumbs else "(unknown)",
            "breadcrumb": crumbs,
            "modified": section.get("data-time-modified", "") if section else "",
            "links": sorted(set(links)),
            "has_content": section is not None,
            "_section": section,
        }
    return index


def _classify(index: dict[str, dict], include: list[str], exclude: list[str]) -> None:
    """Mark every page 'keep' or 'drop' by its breadcrumb root product."""
    inc = {x.strip().lower() for x in include}
    exc = {x.strip().lower() for x in exclude}
    for meta in index.values():
        product = meta["product"].strip().lower()
        if inc:
            meta["status"] = "keep" if product in inc else "drop"
        else:
            meta["status"] = "drop" if product in exc else "keep"


def _rescue_linked(index: dict[str, dict], rescue_depth: int) -> None:
    """Promote dropped pages that kept pages link to, up to `rescue_depth` hops."""
    for _ in range(max(0, rescue_depth)):
        promoted = 0
        for src in [m for m in index.values() if m["status"] in ("keep", "rescued")]:
            for name in src["links"]:
                tgt = index.get(name)
                if tgt and tgt["status"] == "drop":
                    tgt["status"] = "rescued"
                    tgt["rescued_by"] = src["file"]
                    promoted += 1
        if promoted == 0:
            break


def _drop_by_title(index: dict[str, dict], patterns: list[str]) -> int:
    """Force-drop low-value noise (version notes etc.) by title."""
    filters = compile_title_filters(patterns)
    dropped = 0
    for meta in index.values():
        if meta["status"] in ("keep", "rescued") and title_excluded(meta["title"], filters):
            meta["status"] = "drop"
            meta["drop_reason"] = "title-excluded"
            dropped += 1
    return dropped


def _build_records(index: dict[str, dict]) -> tuple[list[dict], int]:
    """Convert kept/rescued bodies to Markdown → (records, duplicates_dropped)."""
    records: list[dict] = []
    seen_bodies: set[str] = set()
    dupes = 0
    for meta in index.values():
        rec = {k: v for k, v in meta.items() if not k.startswith("_")}
        if meta["status"] in ("keep", "rescued") and meta["has_content"]:
            body = _clean_body_md(meta["_section"])
            key = digest(body)
            if key in seen_bodies:
                rec["status"] = "drop"
                rec["drop_reason"] = "duplicate"
                dupes += 1
            else:
                seen_bodies.add(key)
                rec["markdown"] = body
        records.append(rec)
    return records, dupes


def stage_extract(cfg: SimpleNamespace) -> None:
    html_dir = Path(cfg.html_dir)
    manifest_path = html_dir.parent / MANIFEST_FILE
    if manifest_path.exists():
        # Only pages that were live in the nav on the last mirror run. Cached
        # files from renamed or unpublished slugs are left on disk but ignored.
        listed = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = sorted(p for p in (html_dir / rel for rel in listed) if p.exists())
        stale = sum(1 for p in html_dir.rglob("*.html")) - len(files)
        print(f"[extract] using {MANIFEST_FILE}: {len(files)} live pages"
              f"{f', {stale} stale cached files ignored' if stale > 0 else ''}")
    else:
        files = sorted(html_dir.rglob("*.html"))
        print(f"[extract] WARNING: no {MANIFEST_FILE}; reading every cached page, "
              f"including any stale slugs. Run the mirror stage first.", file=sys.stderr)
    if not files:
        print(f"[extract] no .html under {html_dir} — run the mirror stage first.",
              file=sys.stderr)
        sys.exit(1)

    index = _index_pages(files)
    _classify(index, cfg.include, cfg.exclude)
    _rescue_linked(index, 0)          # rescue disabled: mirror prunes at crawl time
    title_dropped = _drop_by_title(index, cfg.title_exclude)
    records, dupes = _build_records(index)

    Path(cfg.json_path).write_text(json.dumps(records, indent=2), encoding="utf-8")

    kept = sum(1 for r in records if r["status"] == "keep")
    resc = sum(1 for r in records if r["status"] == "rescued")
    drop = sum(1 for r in records if r["status"] == "drop")
    unk = sum(1 for r in records if r["product"] == "(unknown)" and r["status"] != "drop")
    print(f"[extract] {len(records)} pages — kept {kept}, rescued {resc}, dropped {drop} "
          f"(title-excluded {title_dropped}, duplicate {dupes})"
          f"{f', unknown-product kept {unk}' if unk else ''}")
    print(f"[extract] wrote {cfg.json_path}")


# ===========================================================================
# STAGE 3 — COMBINE (single canonical markdown)
# ===========================================================================

def _demote_headings(body: str, by: int = 2) -> str:
    """Shift ATX headings deeper (so page content nests under its H3), drop the
    body's leading duplicate H1 (the page title), and leave code fences alone."""
    out, in_code, dropped_h1 = [], False, False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if not in_code:
            mt = re.match(r"^(#{1,6})(\s.*)$", line)
            if mt:
                level = len(mt.group(1))
                if not dropped_h1 and level == 1:
                    dropped_h1 = True
                    continue  # drop duplicate title
                new = min(level + by, 6)
                line = "#" * new + mt.group(2)
        out.append(line)
    return "\n".join(out).strip()


def _page_lines(rec: dict) -> list[str]:
    """One page as an H3 block: title, provenance line, demoted body."""
    meta = []
    if rec["breadcrumb"]:
        meta.append(" › ".join(rec["breadcrumb"]))
    if rec["modified"]:
        meta.append(f"Modified: {rec['modified']}")
    meta.append(f"Source: {rec['file']}")
    return [
        f"### {rec['title']}\n",
        f"_{' · '.join(meta)}_\n",
        _demote_headings(rec["markdown"]),
        "",
    ]


def _newest_modified(records: list[dict]) -> str:
    """Latest per-topic data-time-modified across the kept pages, e.g.
    'September 17, 2026'. This is the honest freshness signal: the Captured
    date is only when the pipeline ran."""
    best = None
    for r in records:
        try:
            d = datetime.strptime(r.get("modified", "").strip(), "%B %d, %Y")
        except ValueError:
            continue
        best = d if best is None or d > best else best
    return f"{best:%B} {best.day}, {best.year}" if best else ""


def stage_combine(cfg: SimpleNamespace) -> None:
    records = json.loads(Path(cfg.json_path).read_text(encoding="utf-8"))
    kept = [r for r in records if r["status"] == "keep" and r.get("markdown")]
    rescued = [r for r in records if r["status"] == "rescued" and r.get("markdown")]

    # Group kept pages by product.
    by_product: dict[str, list] = {}
    for r in kept:
        by_product.setdefault(r["product"], []).append(r)
    for lst in by_product.values():
        lst.sort(key=lambda r: (r["breadcrumb"], r["title"]))

    ordered = order_products(by_product, cfg.product_order)

    date = time.strftime("%Y-%m-%d")
    excluded = ", ".join(cfg.exclude) if not cfg.include else \
        f"(include-only: {', '.join(cfg.include)})"
    lines = []
    lines.append(f"# {cfg.doc_title}\n")
    lines.append(f"**Captured:** {date}  ")
    lines.append(f"**Content current through:** {_newest_modified(kept) or 'unknown'}\n")
    lines.append(f"_Source: {cfg.url}_\n")
    lines.append(f"_Pages: {len(kept)} kept across {len(ordered)} products; "
                 f"{len(rescued)} referenced. Excluded: {excluded}._\n")

    lines += contents_lines(
        ordered, {p: len(by_product[p]) for p in ordered}, len(rescued))

    for p in ordered:
        lines.append(f"## {p}\n")
        # per-section page list for navigability
        for r in by_product[p]:
            lines.append(f"- {r['title']}")
        lines.append("")
        for r in by_product[p]:
            lines += _page_lines(r)

    if rescued:
        lines.append("## Referenced Material\n")
        lines.append("_Pages from excluded products that are cross-referenced "
                     "by kept content (e.g. supported-language tables)._\n")
        rescued.sort(key=lambda r: (r["product"], r["title"]))
        for r in rescued:
            lines += _page_lines(r)

    Path(cfg.md_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    size = Path(cfg.md_path).stat().st_size
    print(f"[combine] wrote {cfg.md_path} — {len(kept)+len(rescued)} pages, "
          f"{size/1_048_576:.2f} MB")


# ===========================================================================
# STAGE 4 — COMPRESS (shrink an existing canonical .md, in place or to a copy)
# ===========================================================================

Block = tuple[str, str]                    # (page title, verbatim block text)


def _split_canonical(text: str) -> tuple[dict[str, list[Block]], list[Block]]:
    """Split a rendered canonical doc back into per-product page blocks.

    Returns (product -> [(title, block)], referenced_blocks). Block text is kept
    verbatim so the re-render never paraphrases or restructures content."""
    sections: dict[str, list[Block]] = {}
    referenced: list[Block] = []
    current_product: str | None = None
    in_referenced = False
    cur_title: str | None = None
    cur_buf: list[str] = []

    def flush() -> None:
        if cur_title is None:
            return
        block = "\n".join(cur_buf).rstrip() + "\n"
        if in_referenced:
            referenced.append((cur_title, block))
        elif current_product is not None:
            sections.setdefault(current_product, []).append((cur_title, block))

    for ln in text.split("\n"):
        if ln.startswith("## "):
            flush()
            cur_title, cur_buf = None, []
            name = ln[3:].strip()
            if name.lower() == "contents":
                current_product, in_referenced = None, False
            elif name.lower() == "referenced material":
                current_product, in_referenced = None, True
            else:
                current_product, in_referenced = name, False
            continue
        if ln.startswith("### "):
            flush()
            cur_title, cur_buf = ln[4:].strip(), []
            continue
        if cur_title is not None:
            cur_buf.append(ln)
    flush()
    return sections, referenced


def stage_compress(cfg: SimpleNamespace) -> None:
    """Post-process an existing canonical doc: drop title-excluded pages and
    exact-duplicate pages, normalize line endings, rebuild Contents + counts."""
    src = Path(cfg.md_path)
    text = src.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")

    h1 = next((ln[2:].strip() for ln in text.split("\n") if ln.startswith("# ")),
              cfg.doc_title)
    sections, referenced = _split_canonical(text)

    # Filter: drop title-excluded + exact-duplicate blocks.
    filters = compile_title_filters(cfg.title_exclude)
    seen: set[str] = set()
    dropped_title = 0
    dropped_dupe = 0

    def keep_block(title: str, block: str) -> bool:
        nonlocal dropped_title, dropped_dupe
        if title_excluded(title, filters):
            dropped_title += 1
            return False
        # Hash the body only — strip the leading _..._ provenance line so that
        # the same content served at different URLs dedupes despite differing Source:.
        body = re.sub(r"^\s*_[^\n]*_\s*\n", "", block, count=1)
        key = digest(body)
        if key in seen:
            dropped_dupe += 1
            return False
        seen.add(key)
        return True

    kept_sections: dict[str, list[Block]] = {}
    for p in order_products(sections, cfg.product_order):
        blocks = [(t, b) for (t, b) in sections[p] if keep_block(t, b)]
        if blocks:
            kept_sections[p] = blocks
    kept_ref = [(t, b) for (t, b) in referenced if keep_block(t, b)]
    ordered = order_products(kept_sections, cfg.product_order)

    total_pages = sum(len(v) for v in kept_sections.values()) + len(kept_ref)

    # Re-render.
    out = [f"# {h1}\n",
           f"**Captured:** {time.strftime('%Y-%m-%d')}\n",
           f"_Source: {cfg.url} (compressed)_\n",
           f"_Pages: {total_pages} across {len(ordered)} products; "
           f"{len(kept_ref)} referenced. "
           f"Removed {dropped_title} version/changelog pages and "
           f"{dropped_dupe} duplicates._\n"]
    out += contents_lines(
        ordered, {p: len(kept_sections[p]) for p in ordered}, len(kept_ref))

    for p in ordered:
        out.append(f"## {p}\n")
        for t, _ in kept_sections[p]:
            out.append(f"- {t}")
        out.append("")
        for t, b in kept_sections[p]:
            out.append(f"### {t}\n")
            out.append(b.rstrip())
            out.append("")
    if kept_ref:
        out.append("## Referenced Material\n")
        for t, b in kept_ref:
            out.append(f"### {t}\n")
            out.append(b.rstrip())
            out.append("")

    dest = Path(cfg.md_out or cfg.md_path)
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    before = src.stat().st_size
    after = dest.stat().st_size
    print(f"[compress] {dest} — {total_pages} pages, "
          f"{before/1_048_576:.2f} MB -> {after/1_048_576:.2f} MB "
          f"({after/before:.0%}); dropped {dropped_title} title-excluded, "
          f"{dropped_dupe} duplicate")


# ===========================================================================
# CLI
# ===========================================================================

def build_cfg(args: argparse.Namespace) -> SimpleNamespace:
    """Merge CONFIG defaults with any command-line overrides.

    Every override defaults to None, so `pick` treats 'not passed' as 'use the
    CONFIG value' while still honouring falsy-but-explicit flags like
    `--include` with no arguments."""
    def pick(value, default):
        return default if value is None else value

    return SimpleNamespace(
        url=pick(args.url, START_URL),
        include=pick(args.include, INCLUDE_PRODUCTS),
        exclude=pick(args.exclude, EXCLUDE_PRODUCTS),
        min_chars=pick(args.min_chars, MIN_CHARS),
        concurrency=pick(args.concurrency, CONCURRENCY),
        depth=pick(args.depth, MAX_DEPTH),
        timeout=pick(args.timeout, NAV_TIMEOUT_MS),
        retries=pick(args.retries, RETRIES),
        wait=pick(args.wait, POLITE_WAIT),
        max_pages=pick(args.max_pages, 0),
        force=args.force,
        html_dir=pick(args.html_dir, HTML_DIR),
        json_path=pick(args.json_path, EXTRACT_JSON),
        md_path=_dated_path(pick(args.md_path, CANONICAL_MD)),
        md_out=args.md_out,
        title_exclude=pick(args.title_exclude, TITLE_EXCLUDE_PATTERNS),
        doc_title=pick(args.doc_title, DOC_TITLE),
        product_order=PRODUCT_ORDER,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Checkmarx docs downloader: user docs (mirror -> extract -> combine) "
                    "and the Checkmarx One API spec (api-spec).")
    ap.add_argument("--stage", choices=["mirror", "extract", "combine", "compress", "docs", "api-spec", "all"],
                    default="all",
                    help="Which stage to run. docs = mirror+extract+combine; "
                         "all (default) = docs + api-spec")
    # overrides (all default None => fall back to CONFIG)
    ap.add_argument("--url")
    ap.add_argument("--include", nargs="*", help="Keep ONLY these product roots")
    ap.add_argument("--exclude", nargs="*", help="Drop these product roots")
    ap.add_argument("--rescue-depth", type=int)
    ap.add_argument("--min-chars", type=int)
    ap.add_argument("--concurrency", type=int)
    ap.add_argument("--depth", type=int)
    ap.add_argument("--timeout", type=int)
    ap.add_argument("--retries", type=int)
    ap.add_argument("--wait", type=float)
    ap.add_argument("--max-pages", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--html-dir")
    ap.add_argument("--json-path")
    ap.add_argument("--md-path")
    ap.add_argument("--md-out", help="Output path for the compress stage (default: overwrite --md-path)")
    ap.add_argument("--title-exclude", nargs="*", help="Regexes; drop pages whose title matches")
    ap.add_argument("--doc-title")
    import cx_api_spec          # no third-party imports at module load, so docs-only runs stay light
    cx_api_spec.add_arguments(ap)
    args = ap.parse_args()

    run_docs = args.stage in ("docs", "all")
    if run_docs or args.stage in ("mirror", "extract", "combine", "compress"):
        cfg = build_cfg(args)
        if args.stage == "mirror" or run_docs:
            stage_mirror(cfg)
        if args.stage == "extract" or run_docs:
            stage_extract(cfg)
        if args.stage == "combine" or run_docs:
            stage_combine(cfg)
        if args.stage == "compress":
            stage_compress(cfg)
    if args.stage in ("api-spec", "all"):
        sys.exit(cx_api_spec.run(cx_api_spec.build_cfg(args)))


if __name__ == "__main__":
    main()
