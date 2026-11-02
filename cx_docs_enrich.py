"""
cx_docs_enrich.py — the deterministic half of the per-page enrichment layer.

An LLM (run by the /enrich-docs skill in Claude Code) writes a short record for
each page: what it is, where it lives, how it is run. This module does everything
around that call, so the LLM output can be trusted only as far as it checks out:

  enrich-prep    find pages whose record is missing or out of date (cache key = page
                 text + prompt version + canonical page for variants), write the
                 instructions and batches of work for the agent, print a cost estimate
  enrich-check   validate the agent's results against the source pages, store the valid
                 ones in the cache, list the rejected ones, and assemble the companion

Companion: <guide>-companion.jsonl, one JSON record per page. It is a separate file;
the guide itself is never changed. A record is only stored when every number, name,
command, path and menu entry it mentions also appears on the source page; anything
else is rejected and the page is retried or left without a record.

A run with no changed pages makes no LLM calls and rewrites the companion identically.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

import cx_docs_analysis as A

PROMPT_VERSION = 1
TYPES = ("concept", "task", "reference", "api", "section")
BATCH_PAGES = 20           # pages per batch
BATCH_CHARS = 120_000      # or this much page text, whichever comes first
BODY_CAP = 24_000          # characters of one page sent to the agent
MIN_WORDS_LLM = 40         # shorter pages need no summary: the page is the summary
MAX_SUMMARY = 600
AGENT_OVERHEAD = 3.5       # billed tokens per page-text token, measured in a 16-page pilot (context is re-read each turn)
ALLOWED_ACRONYMS = {"api", "cli", "ui", "url", "urls", "id", "ids", "ide", "ides", "sdk", "http", "https",
                    "json", "yaml", "ci", "cd", "ai", "sso", "faq", "csv", "pdf", "xml", "ok"}
SYNONYMS_FILE = "docs-synonyms.json"      # optional, curated by a person: {"term": ["alias", ...]}

PROMPT = """\
# Enrichment instructions (prompt version {version})

You are writing a short reference record for each Checkmarx documentation page in a batch file.
The records help software agents find the right page and know what it covers. They are checked
automatically against the page text, so only write what the page itself says.

For every page in the batch, produce one JSON object:

    {{"id": "<id from the batch>",
      "type": "concept | task | reference | api | section",
      "summary": "1-3 sentences: what this is, where it lives (UI location, command or API), and how it is used or run",
      "aliases": ["other names the page itself uses for the same thing, former names, abbreviations"],
      "ui_path": "exact navigation path to the feature if the page gives one, e.g. Settings > Identity and Access Management",
      "cli": ["exact cx commands the page shows"],
      "api_paths": ["exact API paths the page shows"],
      "variant_delta": "only when the page has a variant_of: one sentence on what differs from that page"}}

Rules (a record that breaks them is rejected and the page is retried):
- Use only facts and exact names that appear on the page. Do not use outside knowledge.
- Every number, product or feature name, command, path, flag and menu entry in your text must
  appear verbatim on the page. If you are unsure, leave the field empty ("" or []).
- Do not mention whether something is GA, beta, early access or planned.
- summary: plain prose, no markdown, no more than {max_summary} characters.
- type: task = steps to do something; concept = explains what something is; reference = options,
  parameters, lists or tables; api = API or endpoint documentation; section = a page that mainly
  lists other pages.
- aliases: at most 8, each appearing on the page. Never invent a synonym.
- variant_delta: base it on the `unique_to_page` text in the batch; leave out the field for pages
  without a variant_of.

Output: a JSON array of these objects, one per page in the batch, written to the result file named in
the batch (result-NNN.json). Write nothing else into that file.
"""


def _p(path: str) -> str:
    return os.path.abspath(os.path.normpath(path))


def paths_for(md_path: str) -> dict:
    md_path = _p(md_path)
    base = os.path.dirname(md_path)
    return {"md": md_path, "work": os.path.join(base, "enrich-work"), "cache": os.path.join(base, "enrich-cache"),
            "companion": f"{os.path.splitext(md_path)[0]}-companion.jsonl"}


# ---------------------------------------------------------------------------
# Cache keys and variants
# ---------------------------------------------------------------------------

def text_hash(page: dict) -> str:
    return hashlib.sha256(f"{page['title']}\n{page['body']}".encode("utf-8")).hexdigest()[:16]


def variant_map(pages: list[dict]) -> dict[str, str]:
    """page id -> id of the canonical page it largely repeats (from the analysis clusters)."""
    out = {}
    for c in A.find_clusters(pages)["clusters"]:
        for m in c["members"]:
            if m["id"] != c["canonical"]:
                out[m["id"]] = c["canonical"]
    return out


def page_key(page: dict, by_id: dict, variants: dict[str, str]) -> str:
    """Everything that should trigger a new record: the page text, the prompt, and (for
    a variant) the canonical page's text."""
    canon = by_id.get(variants.get(page["id"], ""))
    raw = f"{PROMPT_VERSION}|{text_hash(page)}|{text_hash(canon) if canon else ''}|{canon['id'] if canon else ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _cache_file(cache_dir: str, pid: str) -> str:
    return os.path.join(cache_dir, f"{pid}.json")


def read_cache(cache_dir: str, pid: str) -> dict | None:
    f = _p(_cache_file(cache_dir, pid))
    if not os.path.isfile(f):
        return None
    try:
        return json.loads(A.read_text(f))
    except ValueError:
        return None


def is_current(entry: dict | None, key: str) -> bool:
    return bool(entry) and entry.get("key") == key and entry.get("prompt_v") == PROMPT_VERSION


def stub_record(page: dict) -> dict:
    """Short pages need no LLM: record that, so they are not retried every month."""
    return {"id": page["id"], "type": "section" if "section-index" in page["flags"] else "reference",
            "summary": "", "aliases": [], "ui_path": "", "cli": [], "api_paths": []}


# ---------------------------------------------------------------------------
# Validation: every checkable token must be on the source page
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_BACKTICK = re.compile(r"`([^`]+)`")
_FLAG = re.compile(r"(?<![\w-])--?[a-z][\w-]*")
_URL = re.compile(r"https?://[^\s)\]>\"']+")
_ABS_PATH = re.compile(r"(?<![\w/.:-])/[a-z][\w-]*(?:/[\w\-{}.:]+)+")
_NAMEY = re.compile(r"\b(?:[A-Z][a-z0-9]+[A-Z]\w*|[A-Z]{2,}[a-z0-9]*|\w+_\w+)\b")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _curly(text: str) -> str:
    return re.sub(r"\{[^}]*\}", "{}", text)


def plain(text: str) -> str:
    """The page text without markdown syntax, so a path written as `**{Base_URL}**/[api/scans](https://...)`
    still reads as `{Base_URL}/api/scans`."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\\([_*\[\]#`<>])", r"\1", text)
    return re.sub(r"[*`]", "", text)


def haystack(page: dict) -> str:
    return _norm(" ".join([page["title"], " ".join(page.get("path") or []), plain(page["body"])]))


def synonyms(path: str) -> set[str]:
    path = _p(path)
    if not os.path.isfile(path):
        return set()
    data = json.loads(A.read_text(path))
    return {_norm(x) for k, v in data.items() for x in [k, *v]}


def untraceable(text: str, hay: str) -> list[str]:
    """Numbers, quoted spans, flags, URLs, paths and name-like tokens in `text` that are not in `hay`."""
    out = []
    hay_c = _curly(hay)
    for n in _NUMBER.findall(text):
        if not re.search(rf"(?<![\d.]){re.escape(n.lower())}(?![\d])", hay):
            out.append(n)
    for span in _BACKTICK.findall(text):
        if _norm(span) not in hay:
            out.append(span)
    for tok in _FLAG.findall(text) + _URL.findall(text):
        if tok.lower() not in hay:
            out.append(tok)
    for pth in _ABS_PATH.findall(text):
        if _curly(pth.lower().rstrip(".,:;)")) not in hay_c:
            out.append(pth)
    for tok in _NAMEY.findall(text):
        if tok.lower() not in ALLOWED_ACRONYMS and not re.search(rf"(?<![\w]){re.escape(tok.lower())}(?![\w])", hay):
            out.append(tok)
    return sorted(set(out))


def validate(rec: dict, page: dict | None, variants: dict[str, str], allowed_aliases: set[str],
             canon: dict | None = None) -> list[str]:
    """Reasons the record cannot be stored; empty when it is good. `canon` is the canonical
    page of a variant: a variant_delta may name things from either page."""
    if page is None:
        return ["unknown page id"]
    problems: list[str] = []
    hay = haystack(page)
    if rec.get("type") not in TYPES:
        problems.append(f"type must be one of {', '.join(TYPES)}")
    summary = rec.get("summary")
    if not isinstance(summary, str) or not 30 <= len(summary) <= MAX_SUMMARY or "\n" in summary:
        problems.append(f"summary must be one paragraph of 30-{MAX_SUMMARY} characters")
    else:
        bad = untraceable(summary, hay)
        if bad:
            problems.append(f"summary mentions text not on the page: {', '.join(bad[:8])}")
    aliases = rec.get("aliases", [])
    if not isinstance(aliases, list) or len(aliases) > 8 or not all(isinstance(a, str) and 0 < len(a) <= 60 for a in aliases):
        problems.append("aliases must be a list of at most 8 short strings")
    else:
        bad = [a for a in aliases if _norm(a) not in hay and _norm(a) not in allowed_aliases]
        if bad:
            problems.append(f"aliases not on the page: {', '.join(bad)}")
    ui = rec.get("ui_path", "")
    if not isinstance(ui, str):
        problems.append("ui_path must be a string")
    else:
        bad = [seg.strip() for seg in re.split(r"›|>|->", ui) if len(seg.strip()) > 1 and _norm(seg) not in hay]
        if bad:
            problems.append(f"ui_path entries not on the page: {', '.join(bad)}")
    for field in ("cli", "api_paths"):
        vals = rec.get(field, [])
        if not isinstance(vals, list) or len(vals) > 12 or not all(isinstance(v, str) for v in vals):
            problems.append(f"{field} must be a list of at most 12 strings")
            continue
        h = _curly(hay) if field == "api_paths" else hay
        bad = [v for v in vals if _norm(_curly(v) if field == "api_paths" else v) not in h]
        if bad:
            problems.append(f"{field} not on the page: {', '.join(bad)}")
    delta = rec.get("variant_delta", "")
    if delta:
        if page["id"] not in variants:
            problems.append("variant_delta given for a page that is not a variant")
        elif not isinstance(delta, str) or len(delta) > 300 or untraceable(
                delta, hay + (" " + haystack(canon) if canon else "")):
            problems.append("variant_delta must be under 300 characters and only use text from the page")
    return problems


def clean_record(rec: dict, page: dict, variants: dict[str, str]) -> dict:
    """The stored shape. variant_of comes from the clusters, never from the agent."""
    out = {"id": page["id"], "type": rec["type"], "summary": rec["summary"].strip(),
           "aliases": sorted(dict.fromkeys(a.strip() for a in rec.get("aliases", []))),
           "ui_path": (rec.get("ui_path") or "").strip(), "cli": list(rec.get("cli", [])),
           "api_paths": list(rec.get("api_paths", []))}
    if page["id"] in variants:
        out["variant_of"] = variants[page["id"]]
        out["variant_delta"] = (rec.get("variant_delta") or "").strip()
    return out


# ---------------------------------------------------------------------------
# Prep
# ---------------------------------------------------------------------------

def _unique_lines(page: dict, canon: dict, cap: int = 2500) -> str:
    seen = {_norm(ln) for ln in canon["body"].split("\n")}
    out = [ln for ln in page["body"].split("\n") if ln.strip() and _norm(ln) not in seen]
    return "\n".join(out)[:cap]


def _write_json(path: str, obj) -> None:
    A.write_text(path, json.dumps(obj, indent=1, ensure_ascii=False))


def prep(md_path: str) -> dict:
    d = paths_for(md_path)
    _, pages = A.load_pages(d["md"])
    by_id = {p["id"]: p for p in pages}
    variants = variant_map(pages)
    os.makedirs(d["work"], exist_ok=True)
    os.makedirs(d["cache"], exist_ok=True)
    for name in os.listdir(d["work"]):                       # old batches are superseded
        if re.fullmatch(r"batch-\d+\.json", name):
            os.remove(os.path.join(d["work"], name))

    jobs, stubs = [], 0
    for p in pages:
        key = page_key(p, by_id, variants)
        if is_current(read_cache(d["cache"], p["id"]), key):
            continue
        if p["words"] < MIN_WORDS_LLM:
            _write_json(_cache_file(d["cache"], p["id"]),
                        {"id": p["id"], "key": key, "prompt_v": PROMPT_VERSION, "record": stub_record(p)})
            stubs += 1
            continue
        job = {"id": p["id"], "title": p["title"], "path": p["path"], "words": p["words"],
               "body": p["body"][:BODY_CAP], "truncated": len(p["body"]) > BODY_CAP}
        if p["id"] in variants:
            canon = by_id[variants[p["id"]]]
            job["variant_of"] = {"id": canon["id"], "title": canon["title"]}
            job["unique_to_page"] = _unique_lines(p, canon)
        jobs.append(job)

    batches, cur, size = [], [], 0
    for j in jobs:
        n = len(j["body"]) + len(j.get("unique_to_page", ""))
        if cur and (len(cur) >= BATCH_PAGES or size + n > BATCH_CHARS):
            batches.append(cur)
            cur, size = [], 0
        cur.append(j)
        size += n
    if cur:
        batches.append(cur)
    for i, b in enumerate(batches, 1):
        _write_json(os.path.join(d["work"], f"batch-{i:03d}.json"),
                    {"prompt_version": PROMPT_VERSION, "result_file": f"result-{i:03d}.json", "pages": b})
    A.write_text(os.path.join(d["work"], "prompt.md"), PROMPT.format(version=PROMPT_VERSION, max_summary=MAX_SUMMARY))
    chars = sum(len(j["body"]) + len(j.get("unique_to_page", "")) for j in jobs)
    prompt_tokens = len(PROMPT) // 4
    return {"pages": len(pages), "to_enrich": len(jobs), "stubs": stubs, "batches": len(batches),
            "est_input_tokens": int((chars // 4 + prompt_tokens * len(batches)) * AGENT_OVERHEAD),
            "est_output_tokens": len(jobs) * 180, "work_dir": d["work"]}


# ---------------------------------------------------------------------------
# Check + companion
# ---------------------------------------------------------------------------

def check(md_path: str, synonyms_file: str = SYNONYMS_FILE) -> dict:
    d = paths_for(md_path)
    _, pages = A.load_pages(d["md"])
    by_id = {p["id"]: p for p in pages}
    variants = variant_map(pages)
    allowed = synonyms(synonyms_file)
    os.makedirs(d["cache"], exist_ok=True)
    os.makedirs(d["work"], exist_ok=True)
    stored, rejected = 0, []
    results = sorted(n for n in os.listdir(d["work"]) if re.fullmatch(r"result-\d+\.json", n))
    for name in results:
        try:
            recs = json.loads(A.read_text(os.path.join(d["work"], name)))
            if not isinstance(recs, list):
                raise ValueError("not a JSON array")
        except ValueError as e:
            rejected.append({"id": "", "file": name, "problems": [f"unreadable result file: {e}"]})
            continue
        for rec in recs:
            page = by_id.get(rec.get("id")) if isinstance(rec, dict) else None
            problems = (validate(rec, page, variants, allowed, by_id.get(variants.get(page["id"], "")))
                        if isinstance(rec, dict) and page else ["not an object or unknown page id"])
            if problems:
                rejected.append({"id": rec.get("id", "") if isinstance(rec, dict) else "", "file": name,
                                 "problems": problems})
                continue
            _write_json(_cache_file(d["cache"], page["id"]),
                        {"id": page["id"], "key": page_key(page, by_id, variants), "prompt_v": PROMPT_VERSION,
                         "record": clean_record(rec, page, variants)})
            stored += 1
    done = os.path.join(d["work"], "done")
    os.makedirs(done, exist_ok=True)
    for name in results:
        os.replace(os.path.join(d["work"], name), os.path.join(done, name))
    _write_json(os.path.join(d["work"], "rejected.json"), rejected)

    lines, missing = [], []
    for p in pages:
        entry = read_cache(d["cache"], p["id"])
        if is_current(entry, page_key(p, by_id, variants)):
            lines.append(json.dumps(entry["record"], ensure_ascii=False, sort_keys=False))
        else:
            missing.append(p["id"])
    A.write_text(d["companion"], "\n".join(lines) + ("\n" if lines else ""))
    return {"stored": stored, "rejected": len(rejected), "companion_records": len(lines), "pages": len(pages),
            "missing": len(missing), "companion": d["companion"]}


def load_companion(md_path: str) -> dict[str, dict]:
    path = paths_for(md_path)["companion"]
    if not os.path.isfile(path):
        return {}
    return {r["id"]: r for r in (json.loads(ln) for ln in A.read_text(path).split("\n") if ln.strip())}
