"""
cx_docs_views.py — compact, agent-oriented views built from the mirror.

  <guide>-reference.md   one entry per page: title, anchor, where it sits, a summary.
                         Pages that repeat a canonical page (one per IDE, per SCM, ...)
                         are listed under it with what differs, instead of in full.
  <guide>-lookup.md      where to find things: terms and aliases, CLI commands, API
                         paths and UI locations, each pointing at page anchors.

Both are small enough to load whole, and every entry links to a page in the full guide.
Summaries come from the companion when there is one (machine-written and checked against
the page); otherwise the first sentence of the page is used. The guide stays authoritative.
Everything here is deterministic: same inputs, same files.
"""

from __future__ import annotations

import json
import os
import re

import cx_docs_analysis as A
import cx_docs_enrich as E

LEAD_CHARS = 220           # fallback summary: the first sentence of the page, cut here
_LINK = re.compile(r"\]\(([^)\s]*#[^)\s]+)\)")
_CX = re.compile(r"^\s*(?:\./|\.\\)?cx(?:\.exe)?\s+(.*)$")


def lead_sentence(body: str) -> str:
    """First prose sentence of a page body, for pages with no companion record."""
    for para in re.split(r"\n\s*\n", body):
        text = para.strip()
        if not text or text.startswith(("#", ">", "```", "|", "- ", "* ", "_", "**")) or re.match(r"\d+\. ", text):
            continue
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", " ".join(text.split()))
        m = re.search(r"(?<=[.!?])\s", text)
        text = text[:m.start()] if m else text
        return text if len(text) <= LEAD_CHARS else text[:LEAD_CHARS].rsplit(" ", 1)[0] + "…"
    return ""


def cli_commands(page: dict) -> set[str]:
    """`cx scan create --flag` lines inside code blocks -> `cx scan create` (words before the first flag or argument)."""
    out: set[str] = set()
    in_code = False
    for line in page["body"].split("\n"):
        if A._FENCE.match(line):
            in_code = not in_code
            continue
        m = _CX.match(line) if in_code else None
        if not m:
            continue
        words = []
        for tok in m.group(1).split():
            if not re.fullmatch(r"[a-z][a-z-]*", tok):
                break
            words.append(tok)
        if words:
            out.add("cx " + " ".join(words[:3]))
    return out


def _ref(page: dict, guide: str) -> str:
    return f"[`{page['id']}`]({page['file']}#{page['id']})"


def reference_markdown(pages: list[dict], companion: dict[str, dict], clusters: dict, title: str) -> str:
    by_id = {p["id"]: p for p in pages}
    members = {m["id"]: c for c in clusters["clusters"] for m in c["members"] if m["id"] != c["canonical"]}
    variants_of: dict[str, list[dict]] = {}
    for pid, c in members.items():
        variants_of.setdefault(c["canonical"], []).append(by_id[pid])
    n_summ = sum(1 for p in pages if companion.get(p["id"], {}).get("summary"))
    lines = [f"# {title} — reference view\n",
             "A compact map of the guide: one entry per page, grouped as the guide is. Every id links to the "
             "full page, which is authoritative. Pages that mostly repeat another page are listed under it "
             "with what differs.\n",
             f"_Summaries: {n_summ} machine-written and checked against the page (marked ✓), the rest are the "
             f"first sentence of the page. {len(pages) - len(members)} entries for {len(pages)} pages._\n"]
    product, section = None, None
    for p in pages:
        if p["id"] in members:
            continue
        crumbs = p["path"]
        if p["product"] != product:
            product, section = p["product"], None
            lines += ["", f"## {product}\n"]
        sec = crumbs[1] if len(crumbs) > 2 else ""
        if sec != section:
            section = sec
            if sec:
                lines += ["", f"### {sec}\n"]
        rec = companion.get(p["id"], {})
        summary = rec.get("summary") or lead_sentence(p["body"])
        mark = " ✓" if rec.get("summary") else ""
        tail = " › ".join(crumbs[2:-1]) if len(crumbs) > 3 else ""
        flags = f" _({', '.join(p['flags'])})_" if p["flags"] else ""
        lines.append(f"- **{p['title']}** {_ref(p, p['file'])}{flags}" + (f" — {tail}" if tail else ""))
        if summary:
            lines.append(f"  {summary}{mark}")
        facts = []
        if rec.get("ui_path"):
            facts.append(f"UI: {rec['ui_path']}")
        if rec.get("cli"):
            facts.append("CLI: " + "; ".join(f"`{c}`" for c in rec["cli"][:4]))
        if rec.get("api_paths"):
            facts.append("API: " + "; ".join(f"`{c}`" for c in rec["api_paths"][:4]))
        if facts:
            lines.append("  " + " · ".join(facts))
        for v in sorted(variants_of.get(p["id"], []), key=lambda x: x["order"]):
            delta = companion.get(v["id"], {}).get("variant_delta", "")
            lines.append(f"  - also: {v['title']} {_ref(v, v['file'])}" + (f" — {delta}" if delta else ""))
    return "\n".join(lines) + "\n"


def lookup_markdown(pages: list[dict], companion: dict[str, dict], spec_file: str, title: str,
                    extra_aliases: dict[str, list[str]] | None = None) -> str:
    by_id = {p["id"]: p for p in pages}

    def refs(ids, cap=8) -> str:
        ids = sorted(dict.fromkeys(ids), key=lambda i: by_id[i]["order"])
        shown = ", ".join(f"{by_id[i]['title']} {_ref(by_id[i], '')}" for i in ids[:cap])
        return shown + (f" (+{len(ids) - cap} more)" if len(ids) > cap else "")

    terms: dict[str, list[str]] = {}
    ui: dict[str, list[str]] = {}
    cli: dict[str, list[str]] = {}
    for p in pages:
        rec = companion.get(p["id"], {})
        for a in rec.get("aliases", []):
            terms.setdefault(a, []).append(p["id"])
        if rec.get("ui_path"):
            ui.setdefault(rec["ui_path"], []).append(p["id"])
        for c in cli_commands(p) | {" ".join(x.split()) for x in rec.get("cli", [])}:
            cli.setdefault(c, []).append(p["id"])
    for term, aliases in (extra_aliases or {}).items():           # curated synonyms: term <-> aliases
        hit = [p["id"] for p in pages if p["title"].lower() == term.lower()]
        for a in aliases:
            terms.setdefault(a, []).extend(hit)
    api = A.api_path_findings(pages, spec_file)

    lines = [f"# {title} — lookup\n",
             "Where to find things in the guide. Each entry points at page ids in the full guide, which is "
             "authoritative.\n"]
    lines += ["## CLI commands\n"]
    lines += [f"- `{c}` — {refs(ids)}" for c, ids in sorted(cli.items())] or ["- none found"]
    lines += ["", "## API paths\n"]
    if api["spec"] is None:
        lines.append("_No API spec found; showing paths with the pages that mention them._")
    lines += [f"- `{r['path']}` ({r['status']}) — {refs(r['pages'])}" for r in api["mentions"]] or ["- none found"]
    if ui:
        lines += ["", "## UI locations\n"]
        lines += [f"- {u} — {refs(ids)}" for u, ids in sorted(ui.items())]
    if terms:
        lines += ["", "## Terms and aliases\n"]
        lines += [f"- {t} — {refs([i for i in ids if i in by_id])}" for t, ids in sorted(terms.items(), key=lambda kv: kv[0].lower())
                  if any(i in by_id for i in ids)]
    return "\n".join(lines) + "\n"


def check_links(text: str, pages: list[dict]) -> list[str]:
    """Links in a view that do not point at a page of the guide (file and anchor must match)."""
    known = {f"{p['file']}#{p['id']}" for p in pages}
    return sorted({t for t in _LINK.findall(text) if t not in known})


def run(md_path: str, spec_file: str, synonyms_file: str = E.SYNONYMS_FILE, title: str = "Checkmarx One docs") -> dict:
    md_path = os.path.abspath(os.path.normpath(md_path))
    idx, pages = A.load_pages(md_path)
    companion = E.load_companion(md_path)
    clusters = A.find_clusters(pages)
    syn_path = os.path.abspath(os.path.normpath(synonyms_file))
    extra = json.loads(A.read_text(syn_path)) if os.path.isfile(syn_path) else {}
    root = os.path.splitext(md_path)[0]
    ref = reference_markdown(pages, companion, clusters, title)
    look = lookup_markdown(pages, companion, spec_file, title, extra)
    A.write_text(f"{root}-reference.md", ref)
    A.write_text(f"{root}-lookup.md", look)
    dead = check_links(ref, pages) + check_links(look, pages)
    total = sum(os.path.getsize(os.path.join(os.path.dirname(md_path), n)) for n in set(idx["files"].values()))
    return {"reference_bytes": len(ref.encode("utf-8")), "lookup_bytes": len(look.encode("utf-8")),
            "guide_bytes": total, "dead_links": len(dead), "dead_examples": dead[:5],
            "summaries": sum(1 for p in pages if companion.get(p["id"], {}).get("summary")), "pages": len(pages)}
