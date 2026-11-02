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
    5. ANALYZE   Cluster near-duplicate pages, flag Upcoming pages that also exist
                 as ordinary pages and API paths missing from the spec (no LLM).
    6. VIEWS     Compact reference view and lookup tables for agents (no LLM).
    7. ENRICH    Optional, run by the /enrich-docs skill: per-page summaries and
                 aliases in a separate companion file, checked against the source.

  API SPEC   (live tenant catalog + Stoplight -> one OpenAPI 3.0.3 candidate)
    api-spec     Fetch both sources, merge them, and report drift against a
                 baseline spec. Everything lands in ./api/ and never mixes
                 with the user-docs files. See cx_api_spec.py for the details.

Outputs go to exactly two folders: docs/ (user docs) and api/ (API spec).
The README lists every file, what it contains and what it is for.

Run everything (default), or any part in isolation:

    python cx_docs_mirror.py                 # docs stages + api-spec
    python cx_docs_mirror.py --stage docs    # mirror -> extract -> combine only
    python cx_docs_mirror.py --stage mirror
    python cx_docs_mirror.py --stage extract
    python cx_docs_mirror.py --stage combine
    python cx_docs_mirror.py --stage compress
    python cx_docs_mirror.py --stage analyze       # clusters + review report from the combined guide
    python cx_docs_mirror.py --stage views         # compact reference view + lookup tables
    python cx_docs_mirror.py --stage enrich-prep   # LLM step 1/3 (then /enrich-docs, then enrich-check)
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

import cx_api_spec
import cx_docs_analysis
import cx_docs_enrich
import cx_docs_render as render
import cx_docs_views

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

# --- Output quality ---------------------------------------------------------
IMAGES = "strip"           # strip (keep meaningful alt text) | absolute (docs.checkmarx.com URLs)
# Pages under these breadcrumb names (or with this title) are API reference that
# overlaps the API spec job; with API_REFERENCE = "separate" they get their own file.
API_REFERENCE = "separate"  # separate | inline
API_REFERENCE_CRUMBS: list[str] = [r"^Checkmarx One API Documentation$", r"REST API Reference$"]
# The CxQL method reference is one ~360 KB page that most readers never need; it gets
# its own file too (CXQL_REFERENCE = "inline" keeps it in the main guide).
CXQL_REFERENCE = "separate"  # separate | inline
CXQL_REFERENCE_PATTERNS: list[str] = [r"^SAST Query Language APIs$"]
STALE_MONTHS = 18          # front-matter index of pages not modified for this long (0 = off)
# Combine fails (exit 3, after writing) when a metric exceeds its limit; None = report only.
QUALITY_LIMITS: dict[str, int | None] = {
    "dead_links": 0, "broken_images": 0, "duplicate_ids": 0, "toggle_leftovers": 0,
    "h6_headings": None, "blockquote_pct": None}

# --- Crawl tuning -----------------------------------------------------------
CONCURRENCY = 8            # simultaneous HTTP requests
MAX_DEPTH = 3              # unused since the nav-driven mirror; kept for CLI compatibility
NAV_TIMEOUT_MS = 45000     # per-request HTTP timeout (ms)
RETRIES = 3
POLITE_WAIT = 0.3          # per-worker delay between pages (seconds)
# Rate limiting (HTTP 429): every worker pauses together, honouring Retry-After
# when the server sends it, then backing off 15s, 30s, 60s... up to 120s.
RATE_LIMIT_RETRIES = 6
RATE_LIMIT_BASE_DELAY = 15.0
RATE_LIMIT_MAX_DELAY = 120.0
RETRY_COOLDOWN = 20.0      # pause before the end-of-run retry pass over failed pages (seconds)
# The mirror stops (exit 4, after writing its files) when more than this share of
# pages could not be checked, so extract/combine never run on silently stale data.
MAX_FAILURE_RATE = 0.02

# --- Output paths -----------------------------------------------------------
# Every user-docs output lives under DOCS_DIR (the API stage writes only to api/).
# The state, manifest, change report and failures.log sit next to the pages
# directory, so they follow it if --html-dir is overridden.
DOCS_DIR = "docs"
HTML_DIR = f"{DOCS_DIR}/pages"                       # cached HTML mirror (pages land in <HTML_DIR>/en/)
EXTRACT_JSON = f"{DOCS_DIR}/cx-extracted.json"       # TEMPORARY: removed after combine (see --keep-temp)
CANONICAL_MD = f"{DOCS_DIR}/checkmarx-one-docs.md"   # final single-file deliverable (dated on write)

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


def _write_text(path, text: str) -> None:
    """Write via a sibling .tmp file, then rename, so an interrupted run never
    leaves a half-written output; the .tmp file is always removed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def _sweep_tmp(root) -> int:
    """Delete .tmp files left behind by a run that was killed mid-write."""
    n = 0
    if Path(root).is_dir():
        for f in Path(root).rglob("*.tmp"):
            f.unlink(missing_ok=True)
            n += 1
    return n


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


def _failure_problem(failures: list, total: int, cfg: SimpleNamespace) -> str | None:
    """A message when too many pages could not be checked, else None."""
    limit = cfg.max_failure_rate
    if limit is None or total == 0 or len(failures) / total <= limit:
        return None
    return (f"{len(failures)} of {total} pages ({100 * len(failures) / total:.1f}%) could not be checked, "
            f"more than the {100 * limit:.1f}% allowed. The files were written, but extract/combine would "
            f"run on stale copies. Re-run later, or raise --max-failure-rate")


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


def _retry_after(r) -> float | None:
    """Seconds from a Retry-After header (the numeric form), capped; None if absent."""
    try:
        return min(float(r.headers.get("retry-after", "")), 300.0)
    except (TypeError, ValueError, AttributeError):
        return None


async def _http_get(client, url: str, cfg: SimpleNamespace, headers: dict):
    """GET with retries and backoff on network errors, 403 and 5xx. A 429 pauses
    EVERY worker (shared cfg.rate), honours Retry-After, and has its own, longer
    retry budget; the per-page delay also grows a little for the rest of the run."""
    rate = getattr(cfg, "rate", None)
    loop = asyncio.get_running_loop()
    last_exc, attempt, throttled = None, 0, 0
    while True:
        if rate is not None and rate["until"] > loop.time():
            await asyncio.sleep(rate["until"] - loop.time())
        try:
            r = await client.get(url, headers=headers)
            if r.status_code in (200, 304, 404, 410):
                return r
            if r.status_code == 429:
                throttled += 1
                last_exc = "HTTP 429"
                if throttled > RATE_LIMIT_RETRIES:
                    break
                delay = _retry_after(r) or min(RATE_LIMIT_MAX_DELAY,
                                               RATE_LIMIT_BASE_DELAY * 2 ** (throttled - 1))
                if rate is not None:
                    if rate["until"] <= loop.time():
                        print(f"      rate limited (429): pausing all requests for {delay:.0f}s")
                    rate["until"] = max(rate["until"], loop.time() + delay)
                    rate["extra"] = min(2.0, rate["extra"] + 0.25)
                else:
                    await asyncio.sleep(delay)
                continue
            last_exc = f"HTTP {r.status_code}"
        except Exception as e:                       # httpx.TransportError etc.
            last_exc = f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"
        attempt += 1
        if attempt >= cfg.retries:
            break
        await asyncio.sleep(2 * attempt)
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
        await asyncio.sleep(cfg.wait + (getattr(cfg, "rate", None) or {}).get("extra", 0.0))

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
            await asyncio.to_thread(_write_text, dest, html)
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
    _write_text(path, "\n".join(lines) + "\n")


async def _mirror(cfg: SimpleNamespace) -> None:
    import httpx

    out_dir = Path(cfg.html_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = out_dir.parent
    _sweep_tmp(base)
    state_path, manifest_path = base / STATE_FILE, base / MANIFEST_FILE
    fail_log = base / "failures.log"
    state: dict = ({} if cfg.fresh else
                   json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {})
    if cfg.fresh:
        print("[mirror] --fresh: ignoring the saved state and re-downloading every page")

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
        cfg.rate = {"until": 0.0, "extra": 0.0}
        await asyncio.gather(*[
            _process(u, p, client, cfg, out_dir, state, sem, stats, changes, failures, manifest)
            for u, p in items])

        if failures and cfg.retry_cooldown >= 0:
            retry = list(failures)
            print(f"[mirror] {len(retry)} pages could not be checked; retrying them once "
                  f"after a {cfg.retry_cooldown:.0f}s cool-down")
            await asyncio.sleep(cfg.retry_cooldown)
            failures.clear()
            stats["failed"] -= len(retry)
            product_of = dict(items)
            for u in retry:
                await _process(u, product_of[u], client, cfg, out_dir, state, sem, stats,
                               changes, failures, manifest)

    for position, (u, _) in enumerate(items):      # sidebar order, used to sort the combined document
        if u in state:
            state[u]["nav"] = position

    # Pages we tracked before that are no longer in the nav (skip when --max-pages
    # truncated the run, since then absence proves nothing).
    removed = []
    if not cfg.max_pages:
        live = set(urls)
        for u in [u for u in state if u not in live]:
            removed.append(state.pop(u) | {"url": u})

    _write_text(state_path, json.dumps(state, indent=2))
    _write_text(manifest_path, json.dumps(sorted(set(manifest)), indent=2))
    report = base / f"changes-{time.strftime('%Y-%m-%d')}.md"
    _write_change_report(report, changes, removed)
    if failures:
        _write_text(fail_log, "\n".join(failures) + "\n")
    else:
        fail_log.unlink(missing_ok=True)      # don't leave last run's failures looking current

    stale = [p for p in out_dir.rglob("*.html")
             if p.relative_to(out_dir).as_posix() not in set(manifest)]
    if cfg.fresh and stale:
        stuck = sum(not cx_api_spec.force_remove(p) for p in stale)
        print(f"[mirror] --fresh: removed {len(stale) - stuck} cached pages that are no longer in the "
              f"nav" + (f"; {stuck} could not be deleted and are ignored" if stuck else ""))
        stale = [p for p in stale if p.exists()]
    dt = time.time() - start
    print(f"\n[mirror] done in {dt/60:.1f} min — new {stats['new']}, updated {stats['updated']}, "
          f"unchanged {stats['unchanged'] + stats['not_modified']} "
          f"(304: {stats['not_modified']}), failed {stats['failed']}")
    print(f"[mirror] removed from nav since last run: {len(removed)}; "
          f"stale cached files ignored by extract: {len(stale)}")
    print(f"[mirror] wrote {manifest_path.name}, {state_path.name}, {report.name}")
    if stats["new"] + stats["updated"] == 0:
        print("[mirror] no content changes detected" + (
            f" among the {len(items) - len(failures)} pages that could be checked." if failures else "."))
    problem = _failure_problem(failures, len(items), cfg)
    if problem:
        print(f"[mirror] ERROR: {problem}", file=sys.stderr)
        sys.exit(4)
    if failures:
        print(f"[mirror] WARNING: {len(failures)} pages could not be checked; their cached copies "
              f"are used (see failures.log)", file=sys.stderr)


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


def _section_text(section) -> str:
    return " ".join(section.get_text(" ", strip=True).split())


def _index_pages(files: list[Path], nav_by_file: dict | None = None) -> dict[str, dict]:
    """Parse every mirrored page into a metadata record keyed by file name.

    The page's BeautifulSoup <section> rides along under '_section' for the
    conversion pass; underscore keys are stripped before serialization."""
    nav_by_file = nav_by_file or {}
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
            "topic_id": render.topic_id(path.name),
            "anchor": render.anchor_for(path.name),
            "nav": nav_by_file.get(path.name),
            "_text_key": digest(_section_text(section)) if section is not None else None,
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


def _is_child(parent: dict, page: dict) -> bool:
    """The breadcrumb ends with the page's own title, so a child's parent trail is the
    parent's whole breadcrumb."""
    return (page is not parent and page["status"] in ("keep", "rescued") and page["has_content"]
            and bool(parent["breadcrumb"]) and page["breadcrumb"][:-1] == parent["breadcrumb"])


def _drop_empty(index: dict[str, dict], images: str) -> int:
    """Pages with no content render to nothing; drop them before links are resolved
    so no link can point at a page that is not in the document. An empty section page
    that has child pages is kept as a 'shell' and later rendered as a list of them."""
    n = 0
    for m in index.values():
        if m["status"] in ("keep", "rescued") and m["has_content"] and render.is_empty(m["_section"], images):
            if any(_is_child(m, c) for c in index.values()):
                m["shell"] = True
                continue
            m["status"], m["drop_reason"] = "drop", "empty"
            n += 1
    return n


def _shell_children(shell: dict, live: list[dict]) -> list[dict]:
    kids = [c for c in live if c["part"] == shell["part"] and _is_child(shell, c)]
    return sorted(kids, key=lambda c: (c["nav"] if c["nav"] is not None else 10 ** 9, c["title"]))


def _resolve_shells(index: dict[str, dict]) -> int:
    """Drop shells whose children did not survive (dedupe, parts); repeat for shells of shells."""
    dropped = 0
    while True:
        live = _live(index)
        gone = [m for m in live if m.get("shell") and not _shell_children(m, live)]
        if not gone:
            return dropped
        for m in gone:
            m["status"], m["drop_reason"] = "drop", "empty"
            dropped += 1


def _live(index: dict[str, dict]) -> list[dict]:
    return [m for m in index.values() if m["status"] in ("keep", "rescued") and m["has_content"]]


def _canonical_first(meta: dict):
    """Sort key: the page that should survive a duplicate group sorts first.
    The original slug beats a `...-<id>` re-publication under another parent;
    then the earlier position in the site navigation; then the shallower path."""
    nav = meta["nav"] if meta["nav"] is not None else 10 ** 9
    return (render.is_reused_slug(meta["file"]), nav, len(meta["breadcrumb"]), meta["file"])


def _collapse(index: dict[str, dict], key_of, reason: str) -> int:
    """Keep one page per key; the rest are dropped and remembered as aliases so
    links to them still resolve."""
    groups: dict[str, list[dict]] = {}
    for m in _live(index):
        k = key_of(m)
        if k:
            groups.setdefault(k, []).append(m)
    dropped = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=_canonical_first)
        keep = members[0]
        for m in members[1:]:
            m["status"], m["drop_reason"], m["duplicate_of"] = "drop", reason, keep["file"]
            keep.setdefault("aliases", []).append(m["file"])
            dropped += 1
    return dropped


def _dedupe(index: dict[str, dict]) -> tuple[int, int]:
    """(duplicate topic ids, identical-text duplicates). The topic id is the
    reliable key; identical visible text is the safety net."""
    # The same topic id can be published with different content per product
    # (Cursor / Windsurf / Kiro variants), so an id alone is not enough to drop a page.
    return (_collapse(index, lambda m: (m["topic_id"], m["_text_key"])
                      if m["topic_id"] and m["_text_key"] else None, "duplicate-topic-id"),
            _collapse(index, lambda m: m["_text_key"], "duplicate"))


def _assign_anchors(live: list[dict]) -> int:
    """Anchors are `p-<topic id>`; when one topic id survives with different
    content, the later variants get `-2`, `-3`... so every anchor stays unique."""
    groups: dict[str, list[dict]] = {}
    for m in live:
        groups.setdefault(m["anchor"], []).append(m)
    n = 0
    for anchor, members in groups.items():
        members.sort(key=_canonical_first)
        for i, m in enumerate(members[1:], 2):
            m["anchor"] = f"{anchor}-{i}"
            n += 1
    return n


def _tag_parts(index: dict[str, dict], mode: str, patterns: list[str], part: str = "api") -> int:
    """Pages that match `patterns` get part=<part> ('api', 'cxql') so combine can write
    them to their own file; with mode 'inline' everything stays in the main document."""
    rx = [re.compile(p, re.I) for p in patterns]
    n = 0
    for m in index.values():
        if mode == "separate" and any(r.search(c) for r in rx for c in [*m["breadcrumb"], m["title"]]):
            m["part"] = part
            n += m["status"] in ("keep", "rescued")
        elif m.get("part") in (None, part):
            m["part"] = "main"
    return n


def _assign_display_titles(live: list[dict]) -> int:
    """Pages that share a title get the breadcrumb tail added: `Title (Parent)`."""
    groups: dict[tuple, list[dict]] = {}
    for m in live:
        groups.setdefault((m["part"], m["title"].strip().lower()), []).append(m)
    changed = 0
    for members in groups.values():
        if len(members) == 1:
            members[0]["display_title"] = members[0]["title"]
            continue
        for depth in range(1, 5):
            labels = []
            for m in members:
                own = m["title"].strip().lower()
                crumbs = [c for c in m["breadcrumb"][1:] if c.strip().lower() != own] or m["breadcrumb"]
                labels.append(f"{m['title']} ({' › '.join(crumbs[-depth:])})" if crumbs else m["title"])
            if len(set(labels)) == len(labels):
                break
        else:
            labels = [f"{m['title']} [{m['topic_id'] or m['file']}]" for m in members]
        for m, label in zip(members, labels):
            m["display_title"] = label
            changed += 1
    return changed


def _shell_markdown(children: list[dict]) -> str:
    """A section page with no text of its own: list its child pages."""
    lines = ["_This section page has no text of its own. Pages in this section:_", ""]
    lines += [f"- [{c['display_title']}](#{c['anchor']})" for c in children]
    return "\n".join(lines)


def _build_records(index: dict[str, dict], cfg: SimpleNamespace):
    """Convert kept pages to Markdown -> (records, render stats)."""
    _resolve_shells(index)
    live = _live(index)
    variants = _assign_anchors(live)
    disambiguated = _assign_display_titles(live)
    target = {m["file"]: {"anchor": m["anchor"], "part": m["part"]} for m in live}
    for m in index.values():                       # a link to an alias lands on the surviving page
        if m.get("duplicate_of") in target:
            target[m["file"]] = target[m["duplicate_of"]]
    ctx = render.RenderCtx(urljoin(cfg.url, "./"), cfg.images, target.get)
    live_ids = {id(m) for m in live}
    records: list[dict] = []
    for meta in index.values():
        rec = {k: v for k, v in meta.items() if not k.startswith("_")}
        if id(meta) in live_ids:
            ctx.part = meta["part"]
            if meta.get("shell"):
                rec["markdown"] = _shell_markdown(_shell_children(meta, live))
                ctx.stats["shell_pages"] += 1
            else:
                rec["markdown"] = render.to_markdown(meta["_section"], ctx)
        records.append(rec)
    ctx.stats["titles_disambiguated"] = disambiguated
    ctx.stats["topic_variants"] = variants
    return records, ctx.stats


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

    state_path = html_dir.parent / STATE_FILE
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    nav = {Path(e["file"]).name: e["nav"] for e in state.values()
           if isinstance(e, dict) and "file" in e and "nav" in e}
    if not nav:
        print("[extract] WARNING: no navigation order in the mirror state; pages fall back to "
              "breadcrumb order. Run the mirror stage once to record it.", file=sys.stderr)

    index = _index_pages(files, nav)
    _classify(index, cfg.include, cfg.exclude)
    _rescue_linked(index, 0)          # rescue disabled: mirror prunes at crawl time
    title_dropped = _drop_by_title(index, cfg.title_exclude)
    empty = _drop_empty(index, cfg.images)
    id_dupes, text_dupes = _dedupe(index)
    api_pages = _tag_parts(index, cfg.api_reference, API_REFERENCE_CRUMBS, "api")
    cxql_pages = _tag_parts(index, cfg.cxql_reference, CXQL_REFERENCE_PATTERNS, "cxql")
    records, rstats = _build_records(index, cfg)

    _write_text(cfg.json_path, json.dumps(records, indent=2))

    kept = sum(1 for r in records if r["status"] == "keep")
    resc = sum(1 for r in records if r["status"] == "rescued")
    drop = sum(1 for r in records if r["status"] == "drop")
    unk = sum(1 for r in records if r["product"] == "(unknown)" and r["status"] != "drop")
    print(f"[extract] {len(records)} pages — kept {kept}, rescued {resc}, dropped {drop} "
          f"(title-excluded {title_dropped}, empty {empty}, duplicate topic id {id_dupes}, "
          f"identical text {text_dupes})"
          f"{f', unknown-product kept {unk}' if unk else ''}")
    print(f"[extract] {api_pages} API-reference and {cxql_pages} CxQL-reference pages routed to their own files; "
          f"{rstats['shell_pages']} section pages list their children, {rstats['accordions']} expandable "
          f"sections unwrapped, {rstats['toggles_removed']} toggle lines removed; "
          f"{rstats['callouts']} callouts, {rstats['tables_as_lists']} tables as lists, "
          f"{rstats['links_internal']} internal links -> anchors, {rstats['links_live_site']} -> live site, "
          f"images: {rstats['images_removed']} removed, {rstats['images_kept_as_text']} kept as text, "
          f"{rstats['images_absolute']} absolute; {rstats['titles_disambiguated']} titles disambiguated, "
          f"{rstats['topic_variants']} same-id variants kept")
    print(f"[extract] wrote {cfg.json_path}")


# ===========================================================================
# STAGE 3 — COMBINE (single canonical markdown)
# ===========================================================================

def _demote_headings(body: str, by: int = 2) -> str:
    """Nest a page's headings under its H3; anything that would pass H6 becomes
    a bold lead-in instead of collapsing into H6 (see cx_docs_render)."""
    return render.demote_headings(body, by)


def _title_of(rec: dict) -> str:
    return rec.get("display_title") or rec["title"]


def _anchor_of(rec: dict) -> str:
    return rec.get("anchor") or render.anchor_for(rec["file"])


def _parse_date(text: str):
    try:
        return datetime.strptime((text or "").strip(), "%B %d, %Y")
    except ValueError:
        return None


def _is_stale(rec: dict, months: int, now: datetime) -> bool:
    d = _parse_date(rec.get("modified", ""))
    return bool(months > 0 and d and (now - d).days > months * 30.44)


def _flags_of(rec: dict, months: int, now: datetime) -> list[str]:
    flags = []
    if _is_stale(rec, months, now):
        flags.append("stale")
    if "Upcoming Features" in rec["breadcrumb"]:
        flags.append("upcoming-section")
    if rec.get("shell"):
        flags.append("section-index")
    return flags


def _page_lines(rec: dict, months: int = 0, now: datetime | None = None) -> list[str]:
    """One page as an H3 block: title with a stable anchor, a fixed header (Path,
    Modified, Source, Flags; one `Key: value` per line), then the body."""
    meta = []
    if rec["breadcrumb"]:
        meta.append(f"Path: {' › '.join(rec['breadcrumb'])}")
    if rec["modified"]:
        meta.append(f"Modified: {rec['modified']}")
    meta.append(f"Source: {rec['file']}")
    flags = _flags_of(rec, months, now or datetime.now())
    if flags:
        meta.append(f"Flags: {', '.join(flags)}")
    return [
        f"### {_title_of(rec)} {{#{_anchor_of(rec)}}}\n",
        "  \n".join(meta) + "\n",
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


def _stale_lines(records: list[dict], months: int, now: datetime) -> list[str]:
    """Front-matter index of pages whose topic was last modified more than
    `months` months ago, oldest first."""
    if months <= 0:
        return []
    stale = []
    for r in records:
        if _is_stale(r, months, now):
            stale.append((_parse_date(r["modified"]), _title_of(r), _anchor_of(r)))
    if not stale:
        return []
    stale.sort()
    lines = [f"**Stale pages** — last modified more than {months} months ago "
             f"({len(stale)} of {len(records)}), oldest first:\n"]
    lines += [f"- [{t}](#{a}) — {d:%B} {d.day}, {d.year}" for d, t, a in stale]
    lines.append("")
    return lines


# Parts of the guide that get their own file: (page tag, file suffix, title suffix, why).
PARTS = (
    ("api", "api-reference", "API Reference Pages", "because they overlap the API spec job"),
    ("cxql", "cxql-reference", "CxQL Reference Pages",
     "because the CxQL method reference is large and rarely needed"),
)
PART_LABELS = {"api": "API-reference", "cxql": "CxQL-reference"}


def _part_path(path: str, part: str) -> str:
    """docs/guide-2026-10-05.md -> docs/guide-2026-10-05-api-reference.md"""
    root, ext = os.path.splitext(path)
    return f"{root}-{part}{ext}"


def _doc_lines(cfg: SimpleNamespace, kept: list[dict], rescued: list[dict], title: str,
               note: str = "") -> list[str]:
    """Render one document (the main guide, or the API-reference part)."""
    by_product: dict[str, list] = {}
    for r in kept:
        by_product.setdefault(r["product"], []).append(r)
    for lst in by_product.values():
        # site navigation order first; breadcrumb/title only break ties or fill gaps
        lst.sort(key=lambda r: (r["nav"] if r.get("nav") is not None else 10 ** 9,
                                r["breadcrumb"], r["title"]))

    ordered = order_products(by_product, cfg.product_order)
    date = time.strftime("%Y-%m-%d")
    excluded = ", ".join(cfg.exclude) if not cfg.include else \
        f"(include-only: {', '.join(cfg.include)})"
    lines = [f"# {title}\n",
             f"**Captured:** {date}  ",
             f"**Content current through:** {_newest_modified(kept) or 'unknown'}\n",
             f"_Source: {cfg.url}_\n",
             f"_Pages: {len(kept)} kept across {len(ordered)} products; "
             f"{len(rescued)} referenced. Excluded: {excluded}._\n"]
    if note:
        lines.append(f"_{note}_\n")
    lines += contents_lines(ordered, {p: len(by_product[p]) for p in ordered}, len(rescued))
    now = datetime.now()
    lines += _stale_lines(kept + rescued, cfg.stale_months, now)

    for p in ordered:
        lines.append(f"## {p}\n")
        for r in by_product[p]:                      # per-section page list for navigability
            lines.append(f"- [{_title_of(r)}](#{_anchor_of(r)})")
        lines.append("")
        for r in by_product[p]:
            lines += _page_lines(r, cfg.stale_months, now)

    if rescued:
        lines.append("## Referenced Material\n")
        lines.append("_Pages from excluded products that are cross-referenced "
                     "by kept content (e.g. supported-language tables)._\n")
        rescued = sorted(rescued, key=lambda r: (r["product"], r["title"]))
        for r in rescued:
            lines += _page_lines(r, cfg.stale_months, now)
    return lines


def _index_path(md_path: str) -> str:
    """docs/guide-2026-10-05.md -> docs/guide-2026-10-05-index.json"""
    return f"{os.path.splitext(md_path)[0]}-index.json"


def _index_json(cfg: SimpleNamespace, jobs: list) -> dict:
    """A machine-readable map of every page in every output file, so an agent can
    find a page (and the file it is in) without loading the guide."""
    now = datetime.now()
    pages, files = [], {}
    for path, label, kept, rescued, _title, _note in jobs:
        files[label] = os.path.basename(path)
        for r in kept + rescued:
            d = _parse_date(r.get("modified", ""))
            pages.append({
                "id": _anchor_of(r), "title": _title_of(r), "path": r["breadcrumb"],
                "product": r["product"], "file": os.path.basename(path), "part": label,
                "source": r["file"], "modified": d.strftime("%Y-%m-%d") if d else "",
                "bytes": len("\n".join(_page_lines(r, cfg.stale_months, now)).encode("utf-8")),
                "flags": _flags_of(r, cfg.stale_months, now), "aliases": sorted(r.get("aliases", [])),
            })
    return {"generated": time.strftime("%Y-%m-%d"), "source": cfg.url, "files": files,
            "pages": sorted(pages, key=lambda p: (p["file"], p["id"]))}


def _check_quality(text: str, label: str, limits: dict) -> tuple[dict, list[str]]:
    m = render.quality_metrics(text, slugify)
    failed = [f"{label}: {k} = {m[k]} (limit {lim})" for k, lim in limits.items()
              if lim is not None and m[k] > lim]
    return m, failed


def stage_combine(cfg: SimpleNamespace) -> None:
    if not Path(cfg.json_path).is_file():
        sys.exit(f"[combine] {cfg.json_path} not found - run the extract stage first")
    records = json.loads(Path(cfg.json_path).read_text(encoding="utf-8"))
    kept_all = [r for r in records if r["status"] == "keep" and r.get("markdown")]
    resc_all = [r for r in records if r["status"] == "rescued" and r.get("markdown")]

    split = {pid: [r for r in kept_all if r.get("part") == pid] for pid, *_ in PARTS}
    main = [r for r in kept_all if r.get("part") not in split]
    notes = []
    for pid, suffix, _, why in PARTS:
        if split[pid]:
            n = len(split[pid])
            notes.append(f"{n} {PART_LABELS[pid]} {'page is' if n == 1 else 'pages are'} in "
                         f"{os.path.basename(_part_path(cfg.md_path, suffix))}, {why}.")
    jobs = [(cfg.md_path, "main", main, resc_all, cfg.doc_title, " ".join(notes))]
    for pid, suffix, label, _ in PARTS:
        if split[pid]:
            jobs.append((_part_path(cfg.md_path, suffix), suffix, split[pid], [], f"{cfg.doc_title} — {label}",
                         f"Split from {os.path.basename(cfg.md_path)}; links to pages in that file "
                         f"point at docs.checkmarx.com."))

    totals: dict = {}
    failures: list[str] = []
    for path, label, kept, rescued, title, jnote in jobs:
        text = "\n".join(_doc_lines(cfg, kept, rescued, title, jnote)) + "\n"
        _write_text(path, text)
        size = os.path.getsize(path)
        metrics, failed = _check_quality(text, label, cfg.quality_limits)
        failures += failed
        for k in ("dead_links", "broken_images", "h6_headings", "duplicate_ids", "toggle_leftovers"):
            totals[k] = totals.get(k, 0) + metrics[k]
        print(f"[combine] wrote {path} — {len(kept) + len(rescued)} pages, {size/1_048_576:.2f} MB")
        if metrics["dead_link_examples"]:
            print(f"[combine]   dead link examples: {', '.join(metrics['dead_link_examples'])}")
    index_path = _index_path(cfg.md_path)
    _write_text(index_path, json.dumps(_index_json(cfg, jobs), indent=1, ensure_ascii=False))
    print(f"[combine] wrote {index_path}")
    print(f"[combine] quality: dead links {totals['dead_links']}, broken images {totals['broken_images']}, "
          f"H6 headings {totals['h6_headings']}, duplicate ids {totals['duplicate_ids']}, "
          f"toggle leftovers {totals['toggle_leftovers']}")
    if failures and not cfg.no_quality_gate:
        print("[combine] QUALITY GATE FAILED (the documents were written; "
              "pass --no-quality-gate to ignore):", file=sys.stderr)
        for f in failures:
            print(f"[combine]   - {f}", file=sys.stderr)
        print(f"[combine] kept {cfg.json_path} for inspection", file=sys.stderr)
        sys.exit(3)
    if not cfg.keep_temp:        # the extract JSON is an intermediate, rebuilt by the next extract
        Path(cfg.json_path).unlink(missing_ok=True)
        print(f"[combine] removed temporary {cfg.json_path} (use --keep-temp to keep it)")


# ===========================================================================
# STAGES 5-8 — ANALYZE / VIEWS / ENRICH (built from the combined output on disk)
# ===========================================================================

def _spec_path() -> str:
    return os.path.join(cx_api_spec.API_OUT_DIR, "cxone_openapi.json")


def _resolve_guide(cfg: SimpleNamespace) -> str:
    """Today's guide when combine has written it, else the newest dated guide that has a
    page index next to it (so a monthly enrich run can happen on a different day)."""
    if os.path.isfile(cx_docs_analysis.index_path(cfg.md_path)):
        return cfg.md_path
    stem = Path(cfg.md_path).stem[:-11]                       # drop "-YYYY-MM-DD"
    folder = Path(cfg.md_path).parent
    found = sorted(folder.glob(f"{stem}-????-??-??-index.json"))
    if not found:
        sys.exit(f"[guide] no combined guide with a page index in {folder} - run the combine stage first")
    return str(found[-1])[: -len("-index.json")] + ".md"


def stage_analyze(cfg: SimpleNamespace) -> None:
    guide = _resolve_guide(cfg)
    r = cx_docs_analysis.run(guide, _spec_path(), cfg.doc_title)
    root = os.path.splitext(guide)[0]
    print(f"[analyze] {r['pages']} pages: {r['clusters']} clusters of near-duplicate pages, {r['contained']} pages "
          f"inside larger pages, {r['upcoming']} Upcoming pages that also exist as ordinary pages, "
          f"{r['missing_api_paths']} API paths not in the spec")
    print(f"[analyze] wrote {root}-clusters.json and {root}-review.md")


def stage_views(cfg: SimpleNamespace) -> None:
    guide = _resolve_guide(cfg)
    r = cx_docs_views.run(guide, _spec_path(), title=cfg.doc_title)
    root = os.path.splitext(guide)[0]
    print(f"[views] {root}-reference.md {r['reference_bytes'] / 1024:.0f} KB "
          f"({100 * r['reference_bytes'] / max(1, r['guide_bytes']):.1f}% of the guide), "
          f"{root}-lookup.md {r['lookup_bytes'] / 1024:.0f} KB; "
          f"{r['summaries']} of {r['pages']} pages have a checked summary")
    if r["dead_links"]:
        print(f"[views] QUALITY GATE FAILED: {r['dead_links']} links do not point at a guide page "
              f"({', '.join(r['dead_examples'])})", file=sys.stderr)
        if not cfg.no_quality_gate:
            sys.exit(3)


def stage_enrich_prep(cfg: SimpleNamespace) -> None:
    guide = _resolve_guide(cfg)
    r = cx_docs_enrich.prep(guide)
    print(f"[enrich-prep] {r['pages']} pages: {r['to_enrich']} need a record in {r['batches']} batches "
          f"({r['stubs']} short pages recorded without an LLM); estimated "
          f"{r['est_input_tokens'] / 1000:.0f}K input and {r['est_output_tokens'] / 1000:.0f}K output tokens")
    if r["to_enrich"]:
        print(f"[enrich-prep] next: run /enrich-docs in Claude Code (batches are in {r['work_dir']}), "
              f"then python cx_docs_mirror.py --stage enrich-check")
    else:
        print("[enrich-prep] nothing to do: every page has a current record")


def stage_enrich_check(cfg: SimpleNamespace) -> None:
    guide = _resolve_guide(cfg)
    r = cx_docs_enrich.check(guide)
    print(f"[enrich-check] stored {r['stored']} new records, rejected {r['rejected']}; companion has "
          f"{r['companion_records']} of {r['pages']} pages ({r['missing']} without a current record) -> {r['companion']}")
    if r["rejected"]:
        print("[enrich-check] rejected records and the reasons are in enrich-work/rejected.json; "
              "run --stage enrich-prep again to retry those pages")


# ===========================================================================
# STAGE 4 — COMPRESS (shrink an existing canonical .md, in place or to a copy)
# ===========================================================================

Block = tuple[str, str]                    # (page title, verbatim block text)


def _strip_anchor(title: str) -> str:
    """`Title {#p-123}` -> `Title`."""
    return re.sub(r"\s*\{#[^}]+\}\s*$", "", title)


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
        # Hash the body only — strip the leading provenance header (Path/Modified/Source/Flags lines, or the older _..._ line) so that
        # the same content served at different URLs dedupes despite differing Source:.
        body = re.sub(r"^\s*(?:_[^\n]*_|(?:(?:Path|Modified|Source|Flags): [^\n]*\n)+)\s*\n", "", block, count=1)
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
            out.append(f"- {_strip_anchor(t)}")
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
    _write_text(dest, "\n".join(out) + "\n")
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
        force=args.force or getattr(args, "fresh", False),
        fresh=getattr(args, "fresh", False),
        html_dir=pick(args.html_dir, HTML_DIR),
        json_path=pick(args.json_path, EXTRACT_JSON),
        md_path=_dated_path(pick(args.md_path, CANONICAL_MD)),
        md_out=args.md_out,
        keep_temp=args.keep_temp,
        max_failure_rate=pick(args.max_failure_rate, MAX_FAILURE_RATE),
        retry_cooldown=RETRY_COOLDOWN,
        images=pick(args.images, IMAGES),
        api_reference=pick(args.api_reference, API_REFERENCE),
        cxql_reference=pick(getattr(args, "cxql_reference", None), CXQL_REFERENCE),
        stale_months=pick(args.stale_months, STALE_MONTHS),
        quality_limits=dict(QUALITY_LIMITS),
        no_quality_gate=args.no_quality_gate,
        title_exclude=pick(args.title_exclude, TITLE_EXCLUDE_PATTERNS),
        doc_title=pick(args.doc_title, DOC_TITLE),
        product_order=PRODUCT_ORDER,
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Checkmarx docs downloader: user docs (mirror -> extract -> combine) "
                    "and the Checkmarx One API spec (api-spec).")
    ap.add_argument("--stage", choices=["mirror", "extract", "combine", "compress", "analyze", "views",
                                           "enrich-prep", "enrich-check", "docs", "api-spec", "all"],
                    default="all",
                    help="Which stage to run. docs = mirror+extract+combine+analyze+views; "
                         "all (default) = docs + api-spec. enrich-prep / enrich-check wrap the "
                         "/enrich-docs skill (LLM step) and are never part of docs or all")
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
    ap.add_argument("--max-failure-rate", type=float,
                    help="Mirror exits 4 when more than this share of pages could not be checked "
                         f"(default {MAX_FAILURE_RATE}; 1 disables)")
    ap.add_argument("--max-pages", type=int)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--html-dir")
    ap.add_argument("--json-path")
    ap.add_argument("--md-path")
    ap.add_argument("--md-out", help="Output path for the compress stage (default: overwrite --md-path)")
    ap.add_argument("--title-exclude", nargs="*", help="Regexes; drop pages whose title matches")
    ap.add_argument("--doc-title")
    ap.add_argument("--images", choices=["strip", "absolute"],
                    help="Images: strip (keep meaningful alt text) or rewrite to docs.checkmarx.com URLs")
    ap.add_argument("--api-reference", choices=["separate", "inline"],
                    help="Write API-reference pages to their own file (default) or keep them inline")
    ap.add_argument("--cxql-reference", choices=["separate", "inline"],
                    help="Write the CxQL method reference to its own file (default) or keep it inline")
    ap.add_argument("--stale-months", type=int, help="Front-matter index of pages not modified for N months (0 = off)")
    ap.add_argument("--no-quality-gate", action="store_true",
                    help="Do not fail combine when a quality metric exceeds its limit")
    ap.add_argument("--keep-temp", action="store_true",
                    help="Keep the temporary extract JSON after combine (deleted by default)")
    cx_api_spec.add_arguments(ap)           # also defines --fresh, which applies to both jobs
    args = ap.parse_args()
    if args.stage in ("api-spec", "all"):          # a typo in --baseline should not cost a 5-minute crawl
        try:
            cx_api_spec.check_inputs(cx_api_spec.build_cfg(args))
        except cx_api_spec.FatalError as e:
            sys.exit(f"[api-spec] FATAL: {e}")

    run_docs = args.stage in ("docs", "all")
    if run_docs or args.stage in ("mirror", "extract", "combine", "compress", "analyze", "views",
                                  "enrich-prep", "enrich-check"):
        cfg = build_cfg(args)
        if args.stage == "mirror" or run_docs:
            stage_mirror(cfg)
        if args.stage == "extract" or run_docs:
            stage_extract(cfg)
        if args.stage == "combine" or run_docs:
            stage_combine(cfg)
        if args.stage == "compress":
            stage_compress(cfg)
        if args.stage == "analyze" or run_docs:
            stage_analyze(cfg)
        if args.stage == "views" or run_docs:
            stage_views(cfg)
        if args.stage == "enrich-prep":
            stage_enrich_prep(cfg)
        if args.stage == "enrich-check":
            stage_enrich_check(cfg)
    if args.stage in ("api-spec", "all"):
        sys.exit(cx_api_spec.run(cx_api_spec.build_cfg(args)))


if __name__ == "__main__":
    main()
