#!/usr/bin/env python3
"""
cx_docs_mirror.py — End-to-end Checkmarx docs pipeline in four stages:

    1. MIRROR    Crawl the docs site (concurrent, verifying, retrying) to HTML.
    2. EXTRACT   Filter by product, rescue cross-linked exceptions, convert the
                 real content of each kept page to faithful Markdown.
    3. COMBINE   Merge everything into ONE canonical .md with a clean heading
                 hierarchy (doc > product > page > page-content).
    4. COMPRESS  Post-process an existing canonical .md, dropping version /
                 changelog pages and duplicates. Not part of the default run.

Run the first three stages (default) or any one in isolation:

    python cx_docs_mirror.py                 # mirror -> extract -> combine
    python cx_docs_mirror.py --stage mirror
    python cx_docs_mirror.py --stage extract
    python cx_docs_mirror.py --stage combine
    python cx_docs_mirror.py --stage compress

All knobs live in the CONFIG block below (target URL, what to include/exclude,
crawl tuning, output paths, product ordering). Anything there can also be
overridden on the command line — see `--help`.

------------------------------------------------------------------------------
SETUP (Python 3.9+):
    python -m pip install -r requirements.txt
    python -m playwright install chromium   # only needed for the mirror stage

Invoke both through `python -m` (`py -m` on Windows). Plain `playwright
install` needs the interpreter's Scripts/bin directory on PATH, which it often
is not; the module form always resolves to the interpreter you just installed
into. Only chromium is used — a bare `playwright install` also fetches Firefox
and WebKit.
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
CONCURRENCY = 12
MAX_DEPTH = 3
NAV_TIMEOUT_MS = 45000
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
# STAGE 1 — MIRROR (concurrent, verifying, retrying)
# ===========================================================================

def stage_mirror(cfg: SimpleNamespace) -> None:
    asyncio.run(_mirror(cfg))


async def _fetch_with_retries(page, url: str, *, retries: int, timeout: int,
                              settle_ms: int = 6000) -> str | None:
    """Load `url`, escalating the timeout on each attempt.

    Early attempts insist on the real content selector; the final attempt
    settles for domcontentloaded plus a fixed wait, which is enough for pages
    that render slowly but are otherwise fine."""
    last_html = None
    for attempt in range(retries):
        is_final = attempt == retries - 1
        t = int(timeout * (1 + 0.5 * attempt))
        try:
            if is_final:
                await page.goto(url, wait_until="domcontentloaded", timeout=t)
                await page.wait_for_timeout(settle_ms)
            else:
                await page.goto(url, wait_until="load", timeout=t)
                await page.wait_for_selector("#topic-content", timeout=t)
            last_html = await page.content()
            return last_html
        except Exception as e:
            if is_final:
                print(f"      attempt {attempt+1}/{retries} failed: "
                      f"{str(e).splitlines()[0]}", file=sys.stderr)
            else:
                await asyncio.sleep(2 * (attempt + 1))
    return last_html


async def _read_cached(dest: Path, url: str, root: str,
                       cfg: SimpleNamespace) -> tuple[str | None, set[str]] | None:
    """Return (product_root, links) for a usable cached page, or None to refetch."""
    if not dest.exists() or cfg.force:
        return None
    existing = await asyncio.to_thread(
        dest.read_text, encoding="utf-8", errors="replace")
    status, product_root, links = await asyncio.to_thread(
        parse_page, existing, url, root, cfg.min_chars)
    return (product_root, links) if status in USABLE_STATUSES else None


async def _fetch_parse(page, url: str, root: str,
                       cfg: SimpleNamespace) -> tuple[str, str | None, str | None, set[str]]:
    """Fetch and parse one page → (status, product_root, html, links).

    Does NOT write to disk; the caller decides whether to save based on the
    breadcrumb product before committing I/O."""
    html = await _fetch_with_retries(
        page, url, retries=cfg.retries, timeout=cfg.timeout)
    if html is None:
        return "shell", None, None, set()
    status, product_root, links = await asyncio.to_thread(
        parse_page, html, url, root, cfg.min_chars)
    if status not in USABLE_STATUSES:
        return status, product_root, None, set()
    return status, product_root, html, links


async def _save_page(html: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(dest.write_text, html, encoding="utf-8")


async def _polite_wait(cfg: SimpleNamespace) -> None:
    if cfg.wait:
        await asyncio.sleep(cfg.wait)


def _is_excluded(product_root: str | None, allowed: set[str]) -> bool:
    """True when this page's breadcrumb product is not in the allowlist.

    When the allowlist is empty no filtering is applied (crawl everything)."""
    if not allowed:
        return False
    return (product_root or "").strip().lower() not in allowed


async def _discover_seed_urls(page, cfg: SimpleNamespace, start_url: str) -> list[str]:
    """Load the docs start page, parse the navigation sidebar, and return root
    URLs for each allowed product.

    Tries several common nav/sidebar CSS selectors. If none match or no seeds
    are found, falls back to [start_url] and breadcrumb-based filtering alone."""
    try:
        await page.goto(start_url, wait_until="domcontentloaded", timeout=cfg.timeout)
        html = await page.content()
    except Exception as e:
        print(f"[mirror] seed discovery failed ({e!s:.80}); "
              f"falling back to start URL", file=sys.stderr)
        return [start_url]

    soup = BeautifulSoup(html, "lxml")
    include_lower = {p.strip().lower() for p in cfg.include}

    # Walk common nav selectors, stop at the first one that exists.
    nav_root = None
    for sel in ("nav", ".sidenav", ".navigation", "#navigation",
                ".toc", "#toc", ".sidebar", "#sidebar"):
        nav_root = (soup.find(sel[1:], class_=sel[1:]) if sel.startswith(".")
                    else soup.find(id=sel[1:]) if sel.startswith("#")
                    else soup.find(sel))
        if nav_root:
            break
    search_scope = nav_root if nav_root else soup

    seeds: list[str] = []
    seen: set[str] = set()
    for a in search_scope.find_all("a", href=True):
        text = a.get_text(strip=True).lower()
        if not any(inc in text or text in inc for inc in include_lower):
            continue
        url, _ = urldefrag(urljoin(start_url, a["href"]))
        if url in seen or not is_in_scope(url, start_url):
            continue
        seen.add(url)
        seeds.append(url)

    if seeds:
        print(f"[mirror] {len(seeds)} seed URL(s) from nav tree:")
        for s in seeds:
            print(f"         {s}")
    else:
        print("[mirror] no nav seeds matched; "
              "falling back to start URL + breadcrumb filtering", file=sys.stderr)
        seeds = [start_url]

    return seeds


async def _worker(ctx, queue, visited, visited_lock, cfg, root, out_dir, stats, failures, stop, allowed_products):
    page = await ctx.new_page()
    try:
        while True:
            url, depth = await queue.get()
            try:
                if stop.is_set():
                    continue

                dest = url_to_path(url, out_dir)
                cached = await _read_cached(dest, url, root, cfg)
                if cached is not None:
                    product_root, links = cached
                    if _is_excluded(product_root, allowed_products):
                        stats["skipped"] += 1
                        print(f"[skip/product] d{depth}  {url}  ({product_root!r})")
                        await _polite_wait(cfg)
                        continue
                    stats["reused"] += 1
                    print(f"[reuse] d{depth}  {url}")
                else:
                    status, product_root, html, links = await _fetch_parse(
                        page, url, root, cfg)
                    if status not in USABLE_STATUSES:
                        stats["failed"] += 1
                        failures.append(url)
                        print(f"  ! FAILED ({status}): {url}", file=sys.stderr)
                        await _polite_wait(cfg)
                        continue
                    if _is_excluded(product_root, allowed_products):
                        stats["skipped"] += 1
                        print(f"[skip/product] d{depth}  {url}  ({product_root!r})")
                        await _polite_wait(cfg)
                        continue
                    await _save_page(html, dest)
                    stats["saved"] += 1
                    if status == "thin":
                        stats["thin"] += 1
                    print(f"[{'save' if status == 'ok' else 'thin'}] d{depth}  {url}")

                if cfg.max_pages and (stats["saved"] + stats["reused"]) >= cfg.max_pages:
                    stop.set()
                    continue

                if depth < cfg.depth:
                    async with visited_lock:
                        new_links = [l for l in links if l not in visited]
                        visited.update(new_links)
                    for link in new_links:
                        queue.put_nowait((link, depth + 1))

                await _polite_wait(cfg)
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        pass
    finally:
        await page.close()


async def _mirror(cfg: SimpleNamespace) -> None:
    from playwright.async_api import async_playwright

    root = cfg.url
    out_dir = Path(cfg.html_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fail_log = out_dir.parent / "failures.log"

    allowed_products = {p.strip().lower() for p in cfg.include}

    visited: set[str] = set()
    visited_lock = asyncio.Lock()
    queue: asyncio.Queue = asyncio.Queue()
    stats = {"saved": 0, "reused": 0, "skipped": 0, "failed": 0, "thin": 0}
    failures: list[str] = []
    stop = asyncio.Event()

    start = time.time()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))

        # Discover seed URLs from the nav tree for the allowed product branches.
        # Each seed is a root URL for one allowed product; crawl workers prune
        # any pages that slip through with a mismatched breadcrumb.
        seed_page = await ctx.new_page()
        seeds = await _discover_seed_urls(seed_page, cfg, root)
        await seed_page.close()
        for seed in seeds:
            if seed not in visited:
                visited.add(seed)
                queue.put_nowait((seed, 0))

        workers = [asyncio.create_task(
            _worker(ctx, queue, visited, visited_lock, cfg, root, out_dir,
                    stats, failures, stop, allowed_products))
            for _ in range(cfg.concurrency)]
        await queue.join()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await browser.close()

    if failures:
        fail_log.write_text("\n".join(failures) + "\n", encoding="utf-8")
    dt = time.time() - start
    print(f"\n[mirror] done in {dt/60:.1f} min — "
          f"saved {stats['saved']} (thin {stats['thin']}), "
          f"reused {stats['reused']}, skipped {stats['skipped']}, "
          f"failed {stats['failed']}")
    if failures:
        print(f"[mirror] {len(failures)} failures logged to {fail_log}; "
              f"re-run --stage mirror to retry just those.")


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
    files = sorted(html_dir.rglob("*.html"))
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
    lines.append(f"**Captured:** {date}\n")
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
        description="Checkmarx docs pipeline: mirror -> extract -> combine.")
    ap.add_argument("--stage", choices=["mirror", "extract", "combine", "compress", "all"],
                    default="all", help="Which stage to run (default: all)")
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
    args = ap.parse_args()
    cfg = build_cfg(args)

    if args.stage in ("mirror", "all"):
        stage_mirror(cfg)
    if args.stage in ("extract", "all"):
        stage_extract(cfg)
    if args.stage in ("combine", "all"):
        stage_combine(cfg)
    if args.stage == "compress":
        stage_compress(cfg)


if __name__ == "__main__":
    main()
