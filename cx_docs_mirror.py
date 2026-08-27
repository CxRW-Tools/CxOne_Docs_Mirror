#!/usr/bin/env python3
"""
cx_docs_mirror.py — End-to-end Checkmarx docs pipeline in three stages:

    1. MIRROR   Crawl the docs site (concurrent, verifying, retrying) to HTML.
    2. EXTRACT  Filter by product, rescue cross-linked exceptions, convert the
                real content of each kept page to faithful Markdown.
    3. COMBINE  Merge everything into ONE canonical .md with a clean heading
                hierarchy (doc > product > page > page-content).

Run all stages (default) or any one in isolation:

    python cx_docs_mirror.py                 # mirror -> extract -> combine
    python cx_docs_mirror.py --stage mirror
    python cx_docs_mirror.py --stage extract
    python cx_docs_mirror.py --stage combine

All knobs live in the CONFIG block below (target URL, what to include/exclude,
crawl tuning, output paths, product ordering). Anything there can also be
overridden on the command line — see `--help`.

------------------------------------------------------------------------------
SETUP:
    pip install playwright beautifulsoup4 lxml markdownify
    playwright install chromium          # only needed for the mirror stage
------------------------------------------------------------------------------
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urljoin, urlparse, urldefrag

from bs4 import BeautifulSoup
from markdownify import markdownify as md

# ===========================================================================
# CONFIG  — edit these defaults; all are overridable via command-line flags.
# ===========================================================================

# Where the crawl starts. This is the page whose directory bounds the crawl.
START_URL = "https://docs.checkmarx.com/en/34965-68517-checkmarx-one-user-guide.html"

# --- What to keep -----------------------------------------------------------
# Classification uses each page's breadcrumb root (the first product node).
# If INCLUDE_PRODUCTS is non-empty, ONLY those products are kept and EXCLUDE is
# ignored. Otherwise everything is kept EXCEPT the products in EXCLUDE_PRODUCTS.
INCLUDE_PRODUCTS: list[str] = []           # e.g. ["Checkmarx One", "Checkmarx DAST"]
EXCLUDE_PRODUCTS: list[str] = [
    "Checkmarx SCA",
    "Checkmarx SAST",
    "SAST/SCA Integrations",
]
# Pages in an excluded product that are LINKED from a kept page get pulled back
# in (e.g. supported-languages tables). 0 disables; 1 = one hop; 2 = two hops.
RESCUE_DEPTH = 1

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


def parse_page(html: str, base_url: str, root: str, min_chars: int):
    """One BeautifulSoup pass → (status, char_count, in_scope_links, soup).
    status: 'ok' | 'thin' | 'shell'."""
    soup = BeautifulSoup(html, "lxml")
    tc = soup.find(id="topic-content")
    section = tc.find("section") if tc else None
    if section is None:
        status, n = "shell", 0
    else:
        n = len(section.get_text(" ", strip=True))
        status = "ok" if n >= min_chars else "thin"
    links = set()
    for a in soup.find_all("a", href=True):
        absolute, _ = urldefrag(urljoin(base_url, a["href"]))
        if is_in_scope(absolute, root):
            links.add(absolute)
    return status, n, links, soup


def slugify(text: str) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_-]+", "-", s) or "section"


# ===========================================================================
# STAGE 1 — MIRROR (concurrent, verifying, retrying)
# ===========================================================================

def stage_mirror(cfg):
    import asyncio
    asyncio.run(_mirror(cfg))


async def _fetch_with_retries(page, url, *, retries, timeout, settle_ms=6000):
    import asyncio
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


async def _worker(ctx, queue, visited, cfg, root, out_dir, stats, failures, stop):
    import asyncio
    page = await ctx.new_page()
    try:
        while True:
            url, depth = await queue.get()
            try:
                if stop.is_set():
                    continue
                dest = url_to_path(url, out_dir)
                html, links = None, set()

                if dest.exists() and not cfg.force:
                    existing = await asyncio.to_thread(
                        dest.read_text, encoding="utf-8", errors="replace")
                    status, _, links, _ = await asyncio.to_thread(
                        parse_page, existing, url, root, cfg.min_chars)
                    if status in ("ok", "thin"):
                        html = existing
                        stats["reused"] += 1
                        print(f"[reuse] d{depth}  {url}")

                if html is None:
                    html = await _fetch_with_retries(
                        page, url, retries=cfg.retries, timeout=cfg.timeout)
                    status = "shell"
                    if html is not None:
                        status, _, links, _ = await asyncio.to_thread(
                            parse_page, html, url, root, cfg.min_chars)
                    if html is not None and status in ("ok", "thin"):
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        await asyncio.to_thread(dest.write_text, html, encoding="utf-8")
                        stats["saved"] += 1
                        if status == "thin":
                            stats["thin"] += 1
                        print(f"[{'save' if status=='ok' else 'thin'}] d{depth}  {url}")
                    else:
                        stats["failed"] += 1
                        failures.append(url)
                        print(f"  ! FAILED ({status}): {url}", file=sys.stderr)
                        if cfg.wait:
                            await asyncio.sleep(cfg.wait)
                        continue

                if cfg.max_pages and (stats["saved"] + stats["reused"]) >= cfg.max_pages:
                    stop.set()
                    continue

                if depth < cfg.depth:
                    for link in links:
                        if link not in visited:
                            visited.add(link)
                            queue.put_nowait((link, depth + 1))

                if cfg.wait:
                    await asyncio.sleep(cfg.wait)
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        pass
    finally:
        await page.close()


async def _mirror(cfg):
    import asyncio
    from playwright.async_api import async_playwright

    root = cfg.url
    out_dir = Path(cfg.html_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fail_log = out_dir.parent / "failures.log"

    visited = {root}
    queue: asyncio.Queue = asyncio.Queue()
    queue.put_nowait((root, 0))
    stats = {"saved": 0, "reused": 0, "failed": 0, "thin": 0}
    failures = []
    stop = asyncio.Event()

    start = time.time()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        workers = [asyncio.create_task(
            _worker(ctx, queue, visited, cfg, root, out_dir, stats, failures, stop))
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
          f"reused {stats['reused']}, failed {stats['failed']}")
    if failures:
        print(f"[mirror] {len(failures)} failures logged to {fail_log}; "
              f"re-run --stage mirror to retry just those.")


# ===========================================================================
# STAGE 2 — EXTRACT (filter + rescue + convert to markdown)
# ===========================================================================

def _breadcrumb(soup):
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


def stage_extract(cfg):
    html_dir = Path(cfg.html_dir)
    files = sorted(html_dir.rglob("*.html"))
    if not files:
        print(f"[extract] no .html under {html_dir} — run the mirror stage first.",
              file=sys.stderr)
        sys.exit(1)

    # Index every page.
    index = {}
    for path in files:
        html = path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        tc = soup.find(id="topic-content")
        section = tc.find("section") if tc else None
        crumbs = _breadcrumb(soup)
        h1 = soup.find("h1")
        modified = section.get("data-time-modified", "") if section else ""
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
            "modified": modified,
            "links": sorted(set(links)),
            "has_content": section is not None,
            "_section": section,
        }

    # Classify.
    inc = {x.strip().lower() for x in cfg.include}
    exc = {x.strip().lower() for x in cfg.exclude}
    for m_ in index.values():
        p = m_["product"].strip().lower()
        if inc:
            m_["status"] = "keep" if p in inc else "drop"
        else:
            m_["status"] = "drop" if p in exc else "keep"

    # Rescue cross-linked excluded pages.
    for _ in range(max(0, cfg.rescue_depth)):
        promoted = 0
        for src in [m_ for m_ in index.values() if m_["status"] in ("keep", "rescued")]:
            for name in src["links"]:
                tgt = index.get(name)
                if tgt and tgt["status"] == "drop":
                    tgt["status"] = "rescued"
                    tgt["rescued_by"] = src["file"]
                    promoted += 1
        if promoted == 0:
            break

    # Title-based exclusion: force-drop low-value noise (version notes etc.)
    title_rx = [re.compile(p, re.I) for p in cfg.title_exclude]
    title_dropped = 0
    for m_ in index.values():
        if m_["status"] in ("keep", "rescued") and any(rx.search(m_["title"]) for rx in title_rx):
            m_["status"] = "drop"
            m_["drop_reason"] = "title-excluded"
            title_dropped += 1

    # Convert kept/rescued bodies; assemble records (dedup identical bodies).
    records = []
    seen_bodies = set()
    dupes = 0
    for m_ in index.values():
        rec = {k: v for k, v in m_.items() if not k.startswith("_")}
        if m_["status"] in ("keep", "rescued") and m_["has_content"]:
            body = _clean_body_md(m_["_section"])
            key = hash(body)
            if key in seen_bodies:
                rec["status"] = "drop"
                rec["drop_reason"] = "duplicate"
                dupes += 1
            else:
                seen_bodies.add(key)
                rec["markdown"] = body
        records.append(rec)

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


def stage_combine(cfg):
    records = json.loads(Path(cfg.json_path).read_text(encoding="utf-8"))
    kept = [r for r in records if r["status"] == "keep" and r.get("markdown")]
    rescued = [r for r in records if r["status"] == "rescued" and r.get("markdown")]

    # Group kept pages by product.
    by_product: dict[str, list] = {}
    for r in kept:
        by_product.setdefault(r["product"], []).append(r)
    for lst in by_product.values():
        lst.sort(key=lambda r: (r["breadcrumb"], r["title"]))

    ordered = [p for p in cfg.product_order if p in by_product]
    ordered += sorted(p for p in by_product if p not in cfg.product_order)

    lines = []
    lines.append(f"# {cfg.doc_title}\n")
    lines.append(f"_Generated {time.strftime('%Y-%m-%d')} from {cfg.url}_\n")
    excluded = ", ".join(cfg.exclude) if not cfg.include else \
        f"(include-only: {', '.join(cfg.include)})"
    lines.append(f"_Pages: {len(kept)} kept across {len(ordered)} products; "
                 f"{len(rescued)} referenced. Excluded: {excluded}._\n")

    # Top-level contents (products only — page lists live in each section).
    lines.append("## Contents\n")
    for p in ordered:
        lines.append(f"- [{p}](#{slugify(p)}) ({len(by_product[p])} pages)")
    if rescued:
        lines.append(f"- [Referenced Material](#referenced-material) "
                     f"({len(rescued)} pages)")
    lines.append("")

    def emit_page(r):
        lines.append(f"### {r['title']}\n")
        meta = []
        if r["breadcrumb"]:
            meta.append(" › ".join(r["breadcrumb"]))
        if r["modified"]:
            meta.append(f"Modified: {r['modified']}")
        meta.append(f"Source: {r['file']}")
        lines.append(f"_{' · '.join(meta)}_\n")
        lines.append(_demote_headings(r["markdown"]))
        lines.append("")

    for p in ordered:
        lines.append(f"## {p}\n")
        # per-section page list for navigability
        for r in by_product[p]:
            lines.append(f"- {r['title']}")
        lines.append("")
        for r in by_product[p]:
            emit_page(r)

    if rescued:
        lines.append("## Referenced Material\n")
        lines.append("_Pages from excluded products that are cross-referenced "
                     "by kept content (e.g. supported-language tables)._\n")
        rescued.sort(key=lambda r: (r["product"], r["title"]))
        for r in rescued:
            emit_page(r)

    Path(cfg.md_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    size = Path(cfg.md_path).stat().st_size
    print(f"[combine] wrote {cfg.md_path} — {len(kept)+len(rescued)} pages, "
          f"{size/1_048_576:.2f} MB")


# ===========================================================================
# STAGE 4 — COMPRESS (shrink an existing canonical .md, in place or to a copy)
# ===========================================================================

def stage_compress(cfg):
    """Post-process an existing canonical doc: drop title-excluded pages and
    exact-duplicate pages, normalize line endings, rebuild Contents + counts.

    Operates on the rendered .md directly (verbatim page blocks are preserved —
    no re-conversion, so nothing is paraphrased or restructured)."""
    src = Path(cfg.md_path)
    text = src.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")

    h1 = next((ln[2:].strip() for ln in text.split("\n") if ln.startswith("# ")),
              cfg.doc_title)

    # Walk lines; track current product (## heading) and split into ### blocks.
    title_rx = [re.compile(p, re.I) for p in cfg.title_exclude]
    sections: dict[str, list] = {}     # product -> list[(title, block_text)]
    referenced: list = []
    current_product = None
    in_referenced = False
    cur_title = None
    cur_buf: list[str] = []

    def flush():
        if cur_title is None:
            return
        block = "\n".join(cur_buf).rstrip() + "\n"
        target = referenced if in_referenced else sections.setdefault(current_product, [])
        if in_referenced:
            referenced.append((cur_title, block))
        else:
            sections.setdefault(current_product, []).append((cur_title, block))

    for ln in text.split("\n"):
        if ln.startswith("## "):
            flush(); cur_title, cur_buf = None, []
            name = ln[3:].strip()
            if name.lower() == "contents":
                current_product = None
            elif name.lower() == "referenced material":
                in_referenced = True; current_product = None
            else:
                in_referenced = False; current_product = name
            continue
        if ln.startswith("### "):
            flush()
            cur_title = ln[4:].strip(); cur_buf = []
            continue
        if cur_title is not None:
            cur_buf.append(ln)
    flush()

    # Filter: drop title-excluded + exact-duplicate blocks.
    seen = set(); dropped_title = 0; dropped_dupe = 0
    def keep_block(title, block):
        nonlocal dropped_title, dropped_dupe
        if any(rx.search(title) for rx in title_rx):
            dropped_title += 1; return False
        # Hash the body only — strip the leading _..._ provenance line so that
        # the same content served at different URLs dedupes despite differing Source:.
        body = re.sub(r"^\s*_[^\n]*_\s*\n", "", block, count=1)
        key = hash(body.strip())
        if key in seen:
            dropped_dupe += 1; return False
        seen.add(key); return True

    ordered = [p for p in cfg.product_order if p in sections]
    ordered += sorted(p for p in sections if p not in cfg.product_order)

    kept_sections = {}
    for p in ordered:
        blocks = [(t, b) for (t, b) in sections[p] if keep_block(t, b)]
        if blocks:
            kept_sections[p] = blocks
    kept_ref = [(t, b) for (t, b) in referenced if keep_block(t, b)]
    ordered = [p for p in ordered if p in kept_sections]

    total_pages = sum(len(v) for v in kept_sections.values()) + len(kept_ref)

    # Re-render.
    out = [f"# {h1}\n",
           f"_Generated {time.strftime('%Y-%m-%d')} from {cfg.url} (compressed)_\n",
           f"_Pages: {total_pages} across {len(ordered)} products; "
           f"{len(kept_ref)} referenced. "
           f"Removed {dropped_title} version/changelog pages and "
           f"{dropped_dupe} duplicates._\n",
           "## Contents\n"]
    for p in ordered:
        out.append(f"- [{p}](#{slugify(p)}) ({len(kept_sections[p])} pages)")
    if kept_ref:
        out.append(f"- [Referenced Material](#referenced-material) ({len(kept_ref)} pages)")
    out.append("")

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

def build_cfg(args) -> SimpleNamespace:
    """Merge CONFIG defaults with any command-line overrides."""
    return SimpleNamespace(
        url=args.url or START_URL,
        include=args.include if args.include is not None else INCLUDE_PRODUCTS,
        exclude=args.exclude if args.exclude is not None else EXCLUDE_PRODUCTS,
        rescue_depth=args.rescue_depth if args.rescue_depth is not None else RESCUE_DEPTH,
        min_chars=args.min_chars if args.min_chars is not None else MIN_CHARS,
        concurrency=args.concurrency if args.concurrency is not None else CONCURRENCY,
        depth=args.depth if args.depth is not None else MAX_DEPTH,
        timeout=args.timeout if args.timeout is not None else NAV_TIMEOUT_MS,
        retries=args.retries if args.retries is not None else RETRIES,
        wait=args.wait if args.wait is not None else POLITE_WAIT,
        max_pages=args.max_pages if args.max_pages is not None else 0,
        force=args.force,
        html_dir=args.html_dir or HTML_DIR,
        json_path=args.json_path or EXTRACT_JSON,
        md_path=args.md_path or CANONICAL_MD,
        md_out=args.md_out,
        title_exclude=args.title_exclude if args.title_exclude is not None else TITLE_EXCLUDE_PATTERNS,
        doc_title=args.doc_title or DOC_TITLE,
        product_order=PRODUCT_ORDER,
    )


def main():
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
