"""
cx_docs_analysis.py — deterministic structure analysis of the combined docs.

Reads the files the combine stage wrote (the page index plus the guide and its
part files) and finds, without any LLM:

  - clusters of near-identical pages (for example one Developer Assist page per IDE)
    with a suggested canonical page, and pages that sit whole inside a larger page
  - pages under "Upcoming Features" that also exist as ordinary pages
  - API paths the guide mentions that the API spec does not contain

Outputs (next to the guide, never mixed into it):
  <guide>-clusters.json   machine-readable clusters and containment
  <guide>-review.md       short report for a human; informational only

Nothing here rewrites page content. The same inputs always give the same outputs.
"""

from __future__ import annotations

import json
import os
import re
import zlib
from itertools import combinations

SHINGLE_WORDS = 8          # words per shingle
MIN_WORDS = 60             # smaller pages are too short to compare reliably
CLUSTER_JACCARD = 0.5      # pages at least this similar are interchangeable variants
CONTAINED = 0.8            # share of the smaller page found inside the larger one

_HEADING = re.compile(r"^### (.*?) \{#([^}\s]+)\}\s*$")
_FENCE = re.compile(r"^\s*(?:[-*+]\s+|\d+\.\s+)?```")
_HEADER_KEY = re.compile(r"^(Path|Modified|Source|Flags): (.*?)\s*$")


# ---------------------------------------------------------------------------
# Reading the combined output back
# ---------------------------------------------------------------------------

def index_path(md_path: str) -> str:
    return f"{os.path.splitext(md_path)[0]}-index.json"


def read_text(path: str) -> str:
    path = os.path.abspath(os.path.normpath(path))
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def write_text(path: str, text: str) -> None:
    """Write via a sibling .tmp file then rename, so an interrupted run leaves no half file."""
    path = os.path.abspath(os.path.normpath(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def split_pages(text: str) -> list[dict]:
    """Pages of one combined file: title, anchor, header fields and body text."""
    pages: list[dict] = []
    cur: dict | None = None
    in_code = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            in_code = not in_code
        m = None if in_code else _HEADING.match(line)
        if m:
            cur = {"title": m.group(1), "id": m.group(2), "header": {}, "lines": []}
            pages.append(cur)
        elif cur is not None:
            cur["lines"].append(line)
    for p in pages:
        lines, header = p.pop("lines"), {}
        i = 0
        while i < len(lines) and not lines[i].strip():
            i += 1
        while i < len(lines):
            k = _HEADER_KEY.match(lines[i])
            if not k:
                break
            header[k.group(1)] = k.group(2)
            i += 1
        p["header"] = header
        p["body"] = "\n".join(lines[i:]).strip()
        p["words"] = len(re.findall(r"\w+", p["body"]))
    return pages


def load_pages(md_path: str) -> tuple[dict, list[dict]]:
    """(index json, pages with bodies) for a combined guide and all of its part files."""
    md_path = os.path.abspath(os.path.normpath(md_path))
    idx = json.loads(read_text(index_path(md_path)))
    base = os.path.dirname(md_path)
    meta = {p["id"]: p for p in idx["pages"]}
    pages: list[dict] = []
    for rank, name in enumerate(dict.fromkeys(idx["files"].values())):      # main guide first, then parts
        for pos, p in enumerate(split_pages(read_text(os.path.join(base, name)))):
            m = meta.get(p["id"])
            if m is None:
                continue
            p.update(file=name, part=m["part"], path=m["path"], product=m["product"], flags=m["flags"],
                     modified=m["modified"], bytes=m["bytes"], order=(rank, pos))
            pages.append(p)
    return idx, sorted(pages, key=lambda p: p["order"])               # the guide's own (navigation) order


# ---------------------------------------------------------------------------
# Clusters
# ---------------------------------------------------------------------------

def shingles(text: str) -> set[int]:
    """Stable 32-bit hashes of every run of SHINGLE_WORDS words (crc32, not hash(), so the
    result does not depend on the process)."""
    w = re.findall(r"\w+", text.lower())
    return {zlib.crc32(" ".join(w[i:i + SHINGLE_WORDS]).encode("utf-8"))
            for i in range(max(0, len(w) - SHINGLE_WORDS + 1))}


def similarities(pages: list[dict]) -> list[tuple[str, str, float, float]]:
    """(id a, id b, jaccard, containment of the smaller in the larger) for pairs that share text."""
    sets = {p["id"]: shingles(p["body"]) for p in pages if p["words"] >= MIN_WORDS}
    owners: dict[int, list[str]] = {}
    for pid, s in sets.items():
        for h in s:
            owners.setdefault(h, []).append(pid)
    shared: dict[tuple[str, str], int] = {}
    for ids in owners.values():
        if len(ids) > 1:
            for a, b in combinations(sorted(ids), 2):
                shared[(a, b)] = shared.get((a, b), 0) + 1
    out = []
    for (a, b), n in sorted(shared.items()):
        la, lb = len(sets[a]), len(sets[b])
        out.append((a, b, n / (la + lb - n), n / min(la, lb)))
    return out


def find_clusters(pages: list[dict]) -> dict:
    by_id = {p["id"]: p for p in pages}
    size = {p["id"]: len(shingles(p["body"])) for p in pages}
    sims = similarities(pages)
    jac: dict[tuple[str, str], float] = {}
    for a, b, j, _ in sims:
        jac[(a, b)] = jac[(b, a)] = j

    # Star clustering: the largest unassigned page seeds a cluster of the pages that are at
    # least CLUSTER_JACCARD similar to it. Unlike chaining, two pages are never joined only
    # because both resemble a third one.
    taken: set[str] = set()
    clusters = []
    for seed in sorted(by_id, key=lambda i: (-by_id[i]["bytes"], i)):
        if seed in taken:
            continue
        members = sorted(m for m in by_id if m not in taken and m != seed and jac.get((seed, m), 0) >= CLUSTER_JACCARD)
        if not members:
            continue
        taken.update(members + [seed])
        everyone = sorted(members + [seed])
        clusters.append({
            "canonical": seed,
            "members": [{"id": m, "title": by_id[m]["title"], "file": by_id[m]["file"], "bytes": by_id[m]["bytes"],
                         "jaccard_to_canonical": 1.0 if m == seed else round(jac[(seed, m)], 3)} for m in everyone],
            "redundant_bytes": sum(by_id[m]["bytes"] for m in members),
        })
    clusters.sort(key=lambda c: (-c["redundant_bytes"], c["canonical"]))
    for i, c in enumerate(clusters, 1):
        c["cluster"] = f"c{i}"

    in_cluster = {m["id"]: c["cluster"] for c in clusters for m in c["members"]}
    contained = []
    for a, b, jac, cont in sims:
        if cont < CONTAINED or jac >= CLUSTER_JACCARD:
            continue
        small, big = (a, b) if size[a] <= size[b] else (b, a)
        if in_cluster.get(small) and in_cluster.get(small) == in_cluster.get(big):
            continue
        contained.append({"page": small, "inside": big, "containment": round(cont, 3), "bytes": by_id[small]["bytes"]})
    contained.sort(key=lambda c: (-c["bytes"], c["page"], c["inside"]))
    return {"params": {"shingle_words": SHINGLE_WORDS, "min_words": MIN_WORDS, "cluster_jaccard": CLUSTER_JACCARD,
                       "contained": CONTAINED},
            "clusters": clusters, "contained": contained}


# ---------------------------------------------------------------------------
# Upcoming Features pages that also exist as ordinary pages
# ---------------------------------------------------------------------------

def _norm_title(title: str) -> str:
    t = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return re.sub(r"^(new|upcoming) ", "", t)


def upcoming_findings(pages: list[dict]) -> list[dict]:
    upcoming = [p for p in pages if "upcoming-section" in p["flags"]]
    others = [p for p in pages if "upcoming-section" not in p["flags"]]
    by_title: dict[str, list[dict]] = {}
    for p in others:
        by_title.setdefault(_norm_title(p["title"]), []).append(p)
    sets = {p["id"]: shingles(p["body"]) for p in others if p["words"] >= MIN_WORDS}
    found = []
    for u in upcoming:
        su = shingles(u["body"]) if u["words"] >= MIN_WORDS else set()
        match, why = None, ""
        if _norm_title(u["title"]) in by_title:
            match, why = by_title[_norm_title(u["title"])][0], "same title"
        elif su:
            best = max(((len(su & s) / min(len(su), len(s)), pid) for pid, s in sets.items() if s), default=(0, ""))
            if best[0] >= 0.5:
                match, why = next(p for p in others if p["id"] == best[1]), f"{best[0]:.0%} of the text is shared"
        if match:
            found.append({"upcoming": u["id"], "title": u["title"], "bytes": u["bytes"],
                          "also": match["id"], "also_title": match["title"], "why": why})
    return sorted(found, key=lambda f: f["upcoming"])


# ---------------------------------------------------------------------------
# API paths in the guide versus the API spec
# ---------------------------------------------------------------------------

_PATH = re.compile(r"(?<![\w/.:-])(/[a-z][a-z0-9-]*(?:/[A-Za-z0-9_\-{}.:]+)+)")


def norm_path(path: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", path).rstrip("/.,:;)")


def mentioned_paths(pages: list[dict], spec_paths: set[str]) -> dict[str, list[str]]:
    """path -> page ids that mention it. Only paths under /api or under a first segment
    the spec uses are considered, so file paths and URLs are not mistaken for endpoints."""
    roots = {p.split("/")[1] for p in spec_paths if p.count("/") >= 1}
    out: dict[str, set[str]] = {}
    for pg in pages:
        for raw in _PATH.findall(pg["body"]):
            if raw.count("{") != raw.count("}"):
                continue
            p = norm_path(raw)
            if p.split("/")[1] in roots or p.startswith("/api/"):
                out.setdefault(p, set()).add(pg["id"])
    return {p: sorted(ids) for p, ids in sorted(out.items())}


def api_path_findings(pages: list[dict], spec_file: str) -> dict:
    spec_file = os.path.abspath(os.path.normpath(spec_file))
    if not os.path.isfile(spec_file):
        return {"spec": None, "mentions": [], "missing": []}
    spec = json.loads(read_text(spec_file))
    spec_paths = {norm_path(p) for p in spec.get("paths", {})}
    rows = []
    for path, ids in mentioned_paths(pages, spec_paths).items():
        if path in spec_paths:
            status = "in spec"
        elif any(s.startswith(path + "/") for s in spec_paths):
            status = "prefix of spec paths"
        else:
            status = "not in spec"
        rows.append({"path": path, "status": status, "pages": ids})
    return {"spec": os.path.basename(spec_file), "mentions": rows,
            "missing": [r for r in rows if r["status"] == "not in spec"]}


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------

def review_markdown(title: str, clusters: dict, upcoming: list[dict], api: dict, pages: list[dict]) -> str:
    by_id = {p["id"]: p for p in pages}
    total = sum(p["bytes"] for p in pages) or 1
    redundant = sum(c["redundant_bytes"] for c in clusters["clusters"])
    lines = [f"# {title} — structure review\n",
             "_Generated by cx_docs_analysis.py. Informational only; nothing in the guide was changed._\n",
             "## Near-duplicate page clusters\n",
             f"{len(clusters['clusters'])} clusters; {redundant:,} bytes ({100 * redundant / total:.1f}% of the pages) "
             f"are in pages that largely repeat a suggested canonical page.\n"]
    for c in clusters["clusters"][:25]:
        names = ", ".join(f"{m['title']} (`{m['id']}`)" for m in c["members"] if m["id"] != c["canonical"])
        lines.append(f"- **{by_id[c['canonical']]['title']}** (`{c['canonical']}`) — repeated by: {names} "
                     f"({c['redundant_bytes']:,} bytes)")
    lines += ["", "## Pages that sit inside a larger page\n",
              f"{len(clusters['contained'])} pages are at least {int(CONTAINED * 100)}% contained in another page. "
              f"Largest:\n"]
    for c in clusters["contained"][:15]:
        lines.append(f"- `{c['page']}` {by_id[c['page']]['title']} ⊂ `{c['inside']}` {by_id[c['inside']]['title']} "
                     f"({c['containment']:.0%})")
    lines += ["", "## Upcoming Features pages that also exist as ordinary pages\n"]
    lines += [f"- `{u['upcoming']}` {u['title']} → `{u['also']}` {u['also_title']} ({u['why']})"
              for u in upcoming] or ["- none"]
    lines += ["", "## API paths mentioned in the guide\n"]
    if api["spec"] is None:
        lines.append("_No API spec found (api/cxone_openapi.json); skipped._")
    else:
        missing = api["missing"]
        lines.append(f"{len(api['mentions'])} distinct paths mentioned; {len(missing)} not in {api['spec']}:\n")
        lines += [f"- `{r['path']}` (pages: {', '.join(r['pages'])})" for r in missing] or ["- none"]
    return "\n".join(lines) + "\n"


def run(md_path: str, spec_file: str, title: str = "Checkmarx One docs") -> dict:
    """Analyse the combined guide on disk and write the clusters file and the review report."""
    md_path = os.path.abspath(os.path.normpath(md_path))
    idx, pages = load_pages(md_path)
    clusters = find_clusters(pages)
    upcoming = upcoming_findings(pages)
    api = api_path_findings(pages, spec_file)
    root = os.path.splitext(md_path)[0]
    out = {"generated": idx.get("generated", ""), **clusters, "upcoming_also_ordinary": upcoming, "api_paths": api}
    write_text(f"{root}-clusters.json", json.dumps(out, indent=1, ensure_ascii=False))
    write_text(f"{root}-review.md", review_markdown(title, clusters, upcoming, api, pages))
    return {"clusters": len(clusters["clusters"]), "contained": len(clusters["contained"]),
            "upcoming": len(upcoming), "missing_api_paths": len(api["missing"]), "pages": len(pages)}
