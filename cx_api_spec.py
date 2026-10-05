#!/usr/bin/env python3
"""
cx_api_spec.py — Checkmarx One API-spec stage for the docs downloader.

Fetches the Checkmarx One API definitions from two public, unauthenticated
sources, merges them into ONE OpenAPI 3.0.3 candidate document, and reports
what changed against a baseline spec. It never overwrites the baseline and it
is kept completely separate from the user-docs outputs (own directory, own
files).

    Source A  live tenant catalog     {base_url}/spec/v1/swagger-starter.js
              -> one OpenAPI YAML per service (authoritative for schemas)
    Source B  Stoplight docs project  checkmarx.stoplight.io public JSON API
              -> one OpenAPI YAML export per API (summaries, descriptions,
                 examples; lags the live tenants)

Run it through cx_docs_mirror.py (`--stage api-spec`) or directly:

    python cx_api_spec.py --baseline path/to/cxone_openapi.json
    python cx_api_spec.py --dry-run          # fetch + report only
    python cx_api_spec.py --from-raw api/raw

Outputs (all inside --api-out, default ./api; the README describes each file):
    raw/                      downloads kept for traceability
      provenance.json         source URL, fetch time, SHA-256 per file
      live/…  stoplight/…
    cxone_openapi.json        merged candidate spec
    api-spec-manifest.json    services, prefix map, unplaced operations
    api-spec-drift.json       machine-readable diff against --baseline
    API-SPEC-REPORT.md        short human summary, highest risk first
    history.json              per-operation signatures between runs

Exit codes: 0 clean / informational drift, 1 fatal (source unreachable or
needs a login), 10 breaking drift touches an endpoint listed in
--used-endpoints, 11 the shrink guard fired (cxone_openapi.json not written).
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlparse

# ===========================================================================
# CONFIG — defaults; every one is overridable from the command line.
# ===========================================================================

API_BASE_URL = "https://ast.checkmarx.net"      # regional tenant root for the live catalog
# Services missing from the catalog that serve their own spec at
# {base_url}/api/{service}/openapi.json (currently need a login: reported, skipped).
API_EXTRA_SERVICES = ["ai-triage", "remediation"]
API_OUT_DIR = "api"                             # everything this stage writes lives here
API_OVERLAY_DIR = "api/overlay"                 # optional hand-maintained operations (input, not output)

STOPLIGHT_HOST = "https://checkmarx.stoplight.io"
STOPLIGHT_WORKSPACE_ID = "d2s6NTE3NDY"          # workspace "checkmarx"
STOPLIGHT_PROJECT_SLUG = "checkmarx-one-api-reference-guide"
STOPLIGHT_PROJECT_ID = "cHJqOjE5ODM2OQ"         # used when slug discovery fails

API_CONCURRENCY = 4                             # hard politeness cap
API_TIMEOUT = 60.0
API_RETRIES = 3
PROBE_DELAY = 0.5                               # seconds between gateway probes
HISTORY_VERSION = 2          # bumped when placement logic changed; older history is discarded
REMOVED_AFTER_RUNS = 2                          # absent from live this many runs in a row

USER_AGENT = ("cxone-docs-mirror/2.0 api-spec "
              "(+https://github.com/CxRW-Tools/CxOne_Docs_Mirror; read-only)")

AUTH_SCHEME = "BearerAuth"                      # every bearer/oauth scheme collapses to this
SERVERS = [
    {"url": "https://ast.checkmarx.net", "description": "US (Primary)"},
    {"url": "https://us.ast.checkmarx.net", "description": "US2"},
    {"url": "https://eu.ast.checkmarx.net", "description": "EU"},
    {"url": "https://eu-2.ast.checkmarx.net", "description": "EU2"},
    {"url": "https://anz.ast.checkmarx.net", "description": "Australia & NZ"},
]

SIBLING_DIR = "stoplight/refs/"      # files only referenced via $ref, not in the table of contents
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
KINDS = ("schemas", "responses", "parameters", "examples", "requestBodies", "headers", "links")
EXIT_FATAL = 1
EXIT_USED_DRIFT = 10
EXIT_SHRINK = 11              # shrink guard fired (see --allow-shrink)
SHRINK_LIMIT = 0.05           # more than 5% fewer operations than the baseline


class FatalError(RuntimeError):
    """A source is unreachable or needs a login; the run cannot continue."""


def _yaml():
    try:
        import yaml
    except ImportError:
        raise FatalError("PyYAML is required for the api-spec stage: "
                         "python -m pip install -r requirements.txt") from None
    return yaml


# ===========================================================================
# Small helpers
# ===========================================================================

def canon(o) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def norm_path(p: str) -> str:
    """Template-insensitive path key: every {param} becomes {}; no trailing '/'."""
    p = re.sub(r"\{[^}]*\}", "{}", (p or "").strip())
    return p.rstrip("/") or "/"


def nkey(method: str, path: str) -> tuple[str, str]:
    return (method.upper(), norm_path(path))


def okey(method: str, path: str) -> str:
    return f"{method.upper()} {path}"


def join_path(prefix: str, path: str) -> str:
    prefix = (prefix or "").rstrip("/")
    if not path or path == "/":
        return prefix or "/"
    return prefix + (path if path.startswith("/") else "/" + path)


def safe_key(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s).strip("_") or "x"


def today() -> str:
    return time.strftime("%Y-%m-%d")


def write_atomic(path: Path, text: str) -> None:
    """Write via a sibling .tmp file, then rename, so an interrupted run never
    leaves a half-written output; the .tmp file is always removed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def sweep_tmp(root: Path) -> None:
    """Delete .tmp files left behind by a run that was killed mid-write."""
    if root.is_dir():
        for f in root.rglob("*.tmp"):
            f.unlink(missing_ok=True)


def dump_json(path: Path, obj) -> None:
    """Deterministic JSON: sorted keys, 2-space indent, trailing newline."""
    write_atomic(path, json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def iter_ops(doc: dict):
    """Yield (path, method, op, path_level_params) in sorted, stable order."""
    paths = doc.get("paths") if isinstance(doc, dict) else None
    for path in sorted(paths or {}):
        item = paths[path]
        if not isinstance(item, dict):
            continue
        for m in METHODS:
            op = item.get(m)
            if isinstance(op, dict):
                yield path, m, op, item.get("parameters")


def server_prefix(url) -> str | None:
    """Gateway prefix from a `servers[].url`. None when it isn't a usable path."""
    if not isinstance(url, str) or not url.strip():
        return None
    u = url.strip()
    if "://" in u:
        u = urlparse(u).path
    elif not u.startswith("/"):
        return None
    if re.search(r"[{}]", u):
        return None
    return u.rstrip("/")


# ===========================================================================
# Raw store: everything downloaded, with provenance
# ===========================================================================

@dataclass
class RawSet:
    """Downloaded files keyed by relative path, plus per-file provenance."""
    entries: dict = field(default_factory=dict)   # relpath -> {url, fetched_at, sha256, status, error}
    blobs: dict = field(default_factory=dict)     # relpath -> bytes

    def put(self, rel: str, data: bytes | None, url: str, status: str = "ok",
            error: str | None = None) -> None:
        prev = self.entries.get(rel, {})
        if status == "ok" and data is not None:
            self.blobs[rel] = data
            self.entries[rel] = {"url": url, "fetched_at": _now(), "sha256": sha256(data),
                                 "status": "ok", "error": None}
        else:  # keep the previous copy (if any) and say why this fetch didn't land
            self.entries[rel] = {**prev, "url": url, "status": status, "error": error}
            if rel not in self.blobs:
                self.entries[rel].setdefault("sha256", None)
                self.entries[rel].setdefault("fetched_at", None)

    def json(self, rel: str):
        blob = self.blobs.get(rel)
        return json.loads(blob.decode("utf-8")) if blob is not None else None

    def save(self, raw_dir: Path) -> None:
        root = raw_dir.resolve()
        for rel, blob in self.blobs.items():
            p = (raw_dir / rel).resolve()
            if root not in p.parents:          # never write outside the raw directory
                raise FatalError(f"refusing to write outside {raw_dir}: {rel!r}")
            if p.exists() and p.read_bytes() == blob:
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            try:
                tmp.write_bytes(blob)
                os.replace(tmp, p)
            finally:
                tmp.unlink(missing_ok=True)
        dump_json(raw_dir / "provenance.json", self.entries)

    @classmethod
    def load(cls, raw_dir: Path) -> "RawSet":
        rs = cls()
        prov = raw_dir / "provenance.json"
        if prov.exists():
            rs.entries = json.loads(prov.read_text(encoding="utf-8"))
        for p in raw_dir.rglob("*"):
            if p.is_file() and p.name != "provenance.json":
                rs.blobs[p.relative_to(raw_dir).as_posix()] = p.read_bytes()
        return rs


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


# ===========================================================================
# Fetching (≤4 concurrent, descriptive UA, timeouts, bounded retries)
# ===========================================================================

def checked_url(url: str, allowed_hosts) -> str:
    """Spec files and Stoplight JSON are remote data, so every URL built from
    them must still point at an expected https host before we request it."""
    u = urlparse(url)
    if u.scheme != "https" or (u.hostname or "").lower() not in allowed_hosts \
            or u.username or u.password or re.search(r"[\s\\]", url):
        raise ValueError(f"refusing to fetch unexpected URL {url!r}")
    return url


async def _get(client, sem, url: str, cfg, headers=None) -> tuple[object | None, str | None]:
    """The single choke point for network reads. GET with bounded retries on
    transport errors / 429 / 5xx. Returns (response, None) or (None, error);
    4xx other than 429 are returned as-is."""
    import httpx
    try:
        checked_url(url, cfg.allowed_hosts)
    except ValueError as e:
        return None, str(e)
    err = None
    for attempt in range(cfg.retries):
        try:
            async with sem:
                # a request carrying credentials must never follow a redirect elsewhere
                r = await client.get(url, timeout=cfg.timeout, headers=headers,
                                     follow_redirects=headers is None)
            if r.status_code == 429 or r.status_code >= 500:
                err = f"HTTP {r.status_code}"
            else:
                return r, None
        except httpx.TransportError as e:
            err = f"{type(e).__name__}: {e}"
        if attempt + 1 < cfg.retries:
            await asyncio.sleep(2 ** attempt)
    return None, err


def _parse_catalog(js: str) -> list[dict]:
    m = re.search(r"urls:\s*(\[.*?\])\s*,\s*\n", js, re.S)
    if not m:
        raise FatalError("swagger-starter.js has no `urls: [...]` array — the page format changed")
    return json.loads(m.group(1))


def _live_key(url: str) -> str:
    """Stable service key: the UPPER_SNAKE tail of the catalog filename."""
    stem = unquote(url.rsplit("/", 1)[-1])
    stem = re.sub(r"\.ya?ml$", "", stem)
    return safe_key(stem.rsplit("-", 1)[-1])


async def fetch_all(cfg, prev: RawSet | None) -> tuple[RawSet, list[str]]:
    """Download both sources. A failed service fetch is recorded and the
    previous raw copy (if any) is kept; it never aborts the run."""
    import httpx
    raw = RawSet()
    if prev:
        raw.entries, raw.blobs = dict(prev.entries), dict(prev.blobs)
    notes: list[str] = []
    sem = asyncio.Semaphore(min(cfg.concurrency, API_CONCURRENCY))
    base = cfg.base_url.rstrip("/")
    headers = {"User-Agent": USER_AGENT}

    async with httpx.AsyncClient(headers=headers, follow_redirects=True,
                                 limits=httpx.Limits(max_connections=API_CONCURRENCY)) as client:
        # ---- Source A: live catalog ---------------------------------------
        starter_url = f"{base}/spec/v1/swagger-starter.js"
        r, err = await _get(client, sem, starter_url, cfg)
        if r is not None and r.status_code in (401, 403):
            raise FatalError(f"{starter_url} requires a login (HTTP {r.status_code}); "
                             "no credential handling is implemented")
        if r is None or r.status_code != 200:
            err = err or f"HTTP {r.status_code}"
            if "live/swagger-starter.js" not in raw.blobs:
                raise FatalError(f"live catalog unreachable: {err}")
            notes.append(f"live catalog fetch failed ({err}); using previous raw copy")
            raw.put("live/swagger-starter.js", None, starter_url, "stale", err)
        else:
            raw.put("live/swagger-starter.js", r.content, starter_url)
        catalog = _parse_catalog(raw.blobs["live/swagger-starter.js"].decode("utf-8"))

        seen: dict[str, int] = {}
        cat_rows = []
        for c in catalog:
            key = _live_key(c["url"])
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                key = f"{key}_{seen[key]}"
            cat_rows.append({"name": c["name"], "key": key, "url": c["url"],
                             "file": f"live/{key}.yaml",
                             "source_url": base + quote(c["url"], safe="/%")})
        cat_rows.sort(key=lambda x: x["key"])
        raw.put("live/catalog.json", (json.dumps(cat_rows, indent=2, sort_keys=True) + "\n").encode(),
                starter_url)

        async def fetch_live(row):
            r, err = await _get(client, sem, row["source_url"], cfg)
            if r is None or r.status_code != 200:
                err = err or f"HTTP {r.status_code}"
                raw.put(row["file"], None, row["source_url"], "failed", err)
                notes.append(f"live {row['key']}: {err}" + (
                    " (previous copy kept)" if row["file"] in raw.blobs else " (no copy available)"))
            else:
                raw.put(row["file"], r.content, row["source_url"])

        async def fetch_extra(svc):
            url = f"{base}/api/{svc}/openapi.json"
            rel = f"live/extra-{safe_key(svc)}.json"
            token = getattr(cfg, "auth_token", None)
            r, err = await _get(client, sem, url, cfg,
                                {"Authorization": f"Bearer {token}"} if token else None)
            if r is None:
                raw.put(rel, None, url, "failed", err)
                notes.append(f"extra service {svc}: {err}")
            elif r.status_code in (401, 403):
                raw.put(rel, None, url, "auth_required", f"HTTP {r.status_code}")
                notes.append(f"extra service {svc}: " + (
                    f"the token was rejected (HTTP {r.status_code}); use a token for the same tenant as "
                    f"{cfg.base_url}" if token else
                    f"needs a login (HTTP {r.status_code}); skipped (see --auth-token-env)"))
            elif r.status_code != 200:
                raw.put(rel, None, url, "failed", f"HTTP {r.status_code}")
                notes.append(f"extra service {svc}: HTTP {r.status_code}")
            else:
                raw.put(rel, r.content, url)

        await asyncio.gather(*[fetch_live(row) for row in cat_rows],
                             *[fetch_extra(s) for s in cfg.extra_services])

        # ---- Source B: Stoplight ------------------------------------------
        await _fetch_stoplight(client, sem, cfg, raw, notes)
    return raw, notes


async def _fetch_stoplight(client, sem, cfg, raw: RawSet, notes: list[str]) -> None:
    api = f"{STOPLIGHT_HOST}/api/v1"

    async def jget(url, what):
        r, err = await _get(client, sem, url, cfg)
        if r is not None and r.status_code in (401, 403):
            raise FatalError(f"Stoplight {what} requires a login (HTTP {r.status_code})")
        if r is None or r.status_code != 200:
            return None, err or f"HTTP {r.status_code}"
        return r, None

    # Resolve the project id from its slug (falls back to the configured id).
    project_id = STOPLIGHT_PROJECT_ID
    r, _ = await jget(f"{api}/workspaces/{STOPLIGHT_WORKSPACE_ID}/projects", "project list")
    if r is not None:
        for p in r.json().get("items", []):
            if p.get("slug") == STOPLIGHT_PROJECT_SLUG:
                project_id = p["id"]
    proj = f"{api}/projects/{project_id}"

    br, err = await jget(f"{proj}/branches", "branches")
    if br is not None:
        raw.put("stoplight/branches.json", br.content, f"{proj}/branches")
    toc_r, err = await jget(f"{proj}/table-of-contents", "table of contents")
    if toc_r is None:
        if "stoplight/toc.json" not in raw.blobs:
            raise FatalError(f"Stoplight table of contents unreachable: {err}")
        notes.append(f"Stoplight TOC fetch failed ({err}); using previous raw copy")
    else:
        raw.put("stoplight/toc.json", toc_r.content, f"{proj}/table-of-contents")
    toc = json.loads(raw.blobs["stoplight/toc.json"])

    services: list[dict] = []

    def walk(items):
        for it in items:
            if it.get("type") == "http_service":
                services.append(it)
            walk(it.get("items", []))
    walk(toc.get("items", []))
    prev_index = {s["id"]: s for s in (raw.json("stoplight/services.json") or [])}

    async def one(s):
        sid = s["id"]
        node_url = f"{proj}/nodes/{s['slug']}"
        r, err = await jget(node_url, f"node {sid}")
        row = None
        if r is not None:
            n = r.json()
            fname = unquote(n["uri"]).lstrip("/")
            row = {"id": sid, "title": n["title"], "slug": s["slug"], "uri": n["uri"],
                   "file": fname, "export_url": n["links"]["export_url"],
                   "relpath": f"stoplight/{safe_key(sid)}__{safe_key(fname)}"}
            if not row["relpath"].endswith((".yaml", ".yml")):
                row["relpath"] += ".yaml"
        elif sid in prev_index:
            row = prev_index[sid]
            notes.append(f"stoplight {s['title']}: {err} (previous index kept)")
        else:
            notes.append(f"stoplight {s['title']}: {err} (no copy available)")
            return None
        r, err = await jget(row["export_url"], f"export {sid}")
        if r is None:
            raw.put(row["relpath"], None, row["export_url"], "failed", err)
            notes.append(f"stoplight {row['title']}: export {err}" + (
                " (previous copy kept)" if row["relpath"] in raw.blobs else " (no copy available)"))
        else:
            raw.put(row["relpath"], r.content, row["export_url"])
        return row

    rows = [r for r in await asyncio.gather(*[one(s) for s in services]) if r]
    rows.sort(key=lambda x: (x["title"], x["id"]))
    raw.put("stoplight/services.json", (json.dumps(rows, indent=2, sort_keys=True) + "\n").encode(),
            f"{proj}/table-of-contents")
    await _fetch_sibling_refs(client, sem, cfg, raw, notes, rows)


_REF_FILE = re.compile(r"\$ref:\s*['\"]?(?:\./)?([^#'\"\s]+?\.ya?ml)#")


async def _fetch_sibling_refs(client, sem, cfg, raw: RawSet, notes: list[str], rows: list[dict]) -> None:
    """Some Stoplight files $ref a sibling file that is not in the table of
    contents (for example sastResults_copy.yaml). Fetch those from the same
    project when they exist; a name the project does not have stays unresolved."""
    if not rows:
        return
    base = rows[0]["export_url"].split("/nodes/")[0]
    known = {r["file"] for r in rows}
    tried: set[str] = set()
    for _ in range(3):                                   # refs of refs
        wanted: set[str] = set()
        for rel, blob in list(raw.blobs.items()):
            if rel.startswith("stoplight/") and rel.endswith((".yaml", ".yml")):
                for m in _REF_FILE.finditer(blob.decode("utf-8", "replace")):
                    name = unquote(m.group(1))
                    if name not in known and name not in tried:
                        wanted.add(name)
        if not wanted:
            return
        for name in sorted(wanted):
            tried.add(name)
            url = f"{base}/nodes/{quote(name)}?fromExportButton=true&snapshotType=http_service"
            rel = f"{SIBLING_DIR}{quote(name, safe='')}"
            r, err = await _get(client, sem, url, cfg)
            if r is not None and r.status_code == 200:
                raw.put(rel, r.content, url)
            else:
                code = err or f"HTTP {r.status_code}"
                raw.put(rel, None, url, "not_found" if r is not None and r.status_code == 404 else "failed", code)
                notes.append(f"Stoplight sibling file {name!r} is referenced but not available ({code}); "
                             "its refs stay unresolved")


# ===========================================================================
# Sources: parsed documents with their declared gateway prefix
# ===========================================================================

@dataclass
class Source:
    origin: str                 # live | stoplight | extra | overlay
    key: str
    name: str
    file: str                   # basename used by cross-file $refs
    relpath: str
    doc: dict
    prefix: str | None = None   # declared gateway prefix ('' = root)
    prefix_src: str = ""
    sl_id: str = ""
    notes: list = field(default_factory=list)


def _strkeys(o):
    """YAML turns `404:` into an int key; $ref pointers and JSON need strings."""
    if isinstance(o, dict):
        return {(k if isinstance(k, str) else str(k)): _strkeys(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_strkeys(x) for x in o]
    return o


def _load_yaml(blob: bytes):
    y = _yaml()
    loader = getattr(y, "CSafeLoader", y.SafeLoader)
    return _strkeys(y.load(blob, Loader=loader))


def _stoplight_prefix(doc: dict) -> tuple[str | None, str, list[str]]:
    """Prefix from the service description ({Base_URL}/api/x), else the
    consensus of its `servers` paths. Stoplight's server lists contain typos
    (e.g. a missing /api), so they are only a fallback."""
    notes = []
    desc = (doc.get("info") or {}).get("description") or ""
    m = re.search(r"<b>\{Base_URL\}(/[^<\s]*)</b>", desc) or \
        re.search(r"\{Base_URL\}(/[^\s<*`)\]\"']*)", desc)
    from_desc = m.group(1).rstrip("/") if m else None
    paths = [server_prefix(s.get("url")) for s in (doc.get("servers") or []) if isinstance(s, dict)]
    paths = [p for p in paths if p is not None]
    from_servers = None
    if paths:
        from_servers = max(sorted(set(paths)), key=paths.count)
    if from_desc is not None:
        if from_servers is not None and from_servers != from_desc:
            notes.append(f"description says {from_desc!r}, servers say {from_servers!r}")
        return from_desc, "stoplight-description", notes
    if from_servers is not None:
        return from_servers, "stoplight-servers", notes
    return None, "", notes


def load_sources(raw: RawSet, notes: list[str]):
    """(live, extra, stoplight, sibling-ref-only) sources."""
    live, extra, sl, refs = [], [], [], []
    for row in raw.json("live/catalog.json") or []:
        blob = raw.blobs.get(row["file"])
        if blob is None:
            continue
        try:
            doc = _load_yaml(blob)
        except _yaml().YAMLError as e:
            notes.append(f"live {row['key']}: unparseable YAML ({str(e)[:80]})")
            continue
        if not isinstance(doc, dict):
            continue
        servers = doc.get("servers") or []
        pfx = server_prefix(servers[0].get("url")) if servers and isinstance(servers[0], dict) else None
        s = Source("live", row["key"], row["name"], row["file"].rsplit("/", 1)[-1], row["file"],
                   doc, pfx, "live-servers" if pfx is not None else "")
        if pfx is None and servers:
            s.notes.append(f"unusable servers[0].url {servers[0].get('url')!r}")
        live.append(s)
    for rel in sorted(raw.blobs):
        if rel.startswith("live/extra-") and rel.endswith(".json"):
            try:
                doc = json.loads(raw.blobs[rel])
            except ValueError:
                continue
            svc = rel[len("live/extra-"):-len(".json")]
            extra.append(Source("extra", f"extra-{svc}", svc, f"extra-{svc}.json", rel, doc,
                                f"/api/{svc}", "extra-service-route"))
    for row in raw.json("stoplight/services.json") or []:
        blob = raw.blobs.get(row["relpath"])
        if blob is None:
            continue
        try:
            doc = _load_yaml(blob)
        except _yaml().YAMLError as e:
            notes.append(f"stoplight {row['title']}: unparseable YAML ({str(e)[:80]})")
            continue
        if not isinstance(doc, dict):
            continue
        pfx, src, n = _stoplight_prefix(doc)
        s = Source("stoplight", row["id"], row["title"], row["file"], row["relpath"], doc, pfx, src,
                   row["id"], n)
        sl.append(s)
    for rel in sorted(raw.blobs):
        if rel.startswith(SIBLING_DIR):
            name = unquote(rel[len(SIBLING_DIR):])
            try:
                doc = _load_yaml(raw.blobs[rel])
            except _yaml().YAMLError:
                continue
            if isinstance(doc, dict):
                refs.append(Source("stoplight", f"ref:{name}", name, name, rel, doc))
    return live, extra, sl, refs


# ===========================================================================
# Reference importer: one merged components section, collision-safe
# ===========================================================================

DROP_KEYS = {"x-stoplight", "x-examples", "$schema", "$id", "$comment", "contentMediaType", "contentEncoding",
             "prefixItems", "unevaluatedProperties", "unevaluatedItems", "$defs", "if", "then", "else"}
OPAQUE_KEYS = {"example", "default", "enum", "const", "value", "externalValue", "security"}
NAME_MAPS = {"properties", "patternProperties", "content", "responses", "headers", "encoding",
             "links", "definitions"}


TYPE_FIXES = {"bool": "boolean", "int": "integer", "float": "number", "str": "string"}


def downgrade(d: dict) -> dict:
    """OpenAPI 3.1 / JSON-Schema-2020 keywords -> 3.0.3 equivalents, plus the
    few structural slips that make a 3.0 document invalid."""
    t = d.get("type")
    if isinstance(t, str) and t in TYPE_FIXES:
        d["type"] = t = TYPE_FIXES[t]
    if isinstance(d.get("required"), bool) and not (
            {"in", "name"} <= d.keys() or "content" in d or "schema" in d):
        d.pop("required")                  # `required: false` on a schema is meaningless
    exs = d.get("examples")
    if isinstance(exs, dict) and "example" in d:   # 3.0: example / examples are exclusive
        if exs:
            d.pop("example")
        else:
            d.pop("examples")
    if isinstance(t, list):
        non_null = [x for x in t if x != "null"]
        if len(non_null) != len(t):
            d["nullable"] = True
        if len(non_null) == 1:
            d["type"] = non_null[0]
        elif not non_null:
            d.pop("type")
        else:
            d.pop("type")
            d.setdefault("anyOf", [{"type": x} for x in non_null])
    if "const" in d:
        c = d.pop("const")
        d.setdefault("enum", [c])
    for k, base in (("exclusiveMinimum", "minimum"), ("exclusiveMaximum", "maximum")):
        v = d.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            d[base] = v
            d[k] = True
    ex = d.get("examples")
    if isinstance(ex, list):
        d.pop("examples")
        if "example" not in d and ex:
            d["example"] = ex[0]
    for k in ("anyOf", "oneOf"):
        v = d.get(k)
        if isinstance(v, list):
            rest = [x for x in v if not (isinstance(x, dict) and x == {"type": "null"})]
            if len(rest) != len(v):
                d["nullable"] = True
                if rest:
                    d[k] = rest
                else:
                    d.pop(k)
    return d


class Importer:
    """Owns the merged `components` and the unresolved-ref log."""

    def __init__(self):
        self.components = {k: {} for k in KINDS}
        self.security_schemes: dict[str, dict] = {}
        self.hashes: dict = {}
        self.hash_memo: dict = {}
        self.unresolved: list[dict] = []
        self.by_file: dict[tuple[str, str], "Ctx"] = {}
        self.inline_stack: set = set()
        self.synthesized: list[dict] = []

    def ctx(self, src: Source) -> "Ctx":
        c = Ctx(self, src)
        self.by_file[(src.origin, src.file.lower())] = c
        return c


class Ctx:
    """Rewrites one source document's content into the merged namespace."""

    def __init__(self, imp: Importer, src: Source):
        self.imp, self.src, self.doc = imp, src, src.doc
        self.memo: dict = {}

    # -- reference plumbing ------------------------------------------------
    def _split(self, ref: str):
        if "__bundled__" in ref or ref.startswith(("http://", "https://")):
            return None
        file_part, _, frag = ref.partition("#")
        ctx = self
        if file_part:
            base = unquote(file_part).replace("\\", "/").rsplit("/", 1)[-1].lower()
            ctx = self.imp.by_file.get((self.src.origin, base))
            if ctx is None:
                return None
        segs = [unquote(s).replace("~1", "/").replace("~0", "~")
                for s in frag.strip("/").split("/")] if frag.strip("/") else []
        return ctx, segs

    @staticmethod
    def _walk(doc, segs):
        node = doc
        for s in segs:
            if isinstance(node, dict) and s in node:
                node = node[s]
            elif isinstance(node, list) and s.isdigit() and int(s) < len(node):
                node = node[int(s)]
            else:
                return None
        return node

    def peek(self, ref: str):
        r = self._split(ref)
        if r is None:
            return None
        ctx, segs = r
        return self._walk(ctx.doc, segs)

    def _unresolved(self, ref: str) -> dict:
        self.imp.unresolved.append({"source": self.src.name, "origin": self.src.origin, "ref": ref})
        m = re.search(r"/components/(\w+)/", ref)
        kind = m.group(1) if m else "schemas"
        marker: dict = {"x-unresolved-ref": ref}
        if kind == "responses":
            marker["description"] = "Unresolved reference"
        elif kind == "requestBodies":
            marker["content"] = {}
        return marker

    def closure_hash(self, kind: str, name: str, stack: tuple) -> tuple[str, bool]:
        key = (id(self), kind, name)
        if key in self.imp.hash_memo:
            return self.imp.hash_memo[key], False
        if key in stack:
            return f"cycle:{kind}/{name}", True
        raw = ((self.doc.get("components") or {}).get(kind) or {}).get(name)
        cyc = False

        def sub(node):
            nonlocal cyc
            if isinstance(node, list):
                return [sub(x) for x in node]
            if not isinstance(node, dict):
                return node
            ref = node.get("$ref")
            if isinstance(ref, str):
                r = self._split(ref)
                if r is None:
                    return {"$unres": ref}
                ctx, segs = r
                if len(segs) == 3 and segs[0] == "components" and segs[1] in KINDS:
                    h, c = ctx.closure_hash(segs[1], segs[2], stack + (key,))
                    cyc = cyc or c
                    return {"$h": h}
                return {"$ptr": ref}
            return {k: sub(v) for k, v in node.items() if k not in DROP_KEYS}

        h = sha256(canon(sub(raw)))
        if not cyc:
            self.imp.hash_memo[key] = h
        return h, cyc

    def import_component(self, kind: str, name: str) -> str | None:
        key = (kind, name)
        if key in self.memo:
            return self.memo[key]
        raw = ((self.doc.get("components") or {}).get(kind) or {}).get(name)
        if raw is None:
            self.memo[key] = None
            return None
        h, _ = self.closure_hash(kind, name, ())
        base = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "x"
        label = safe_key(self.src.key)
        comps = self.imp.components[kind]
        for cand in [base, f"{base}_{label}"] + [f"{base}_{label}_{i}" for i in range(2, 50)]:
            if cand not in comps:
                comps[cand] = {}
                self.imp.hashes[(kind, cand)] = h
                self.memo[key] = cand
                comps[cand] = self.rewrite(raw)
                return cand
            if self.imp.hashes.get((kind, cand)) == h:
                self.memo[key] = cand
                return cand
        raise RuntimeError(f"cannot place component {kind}/{name}")

    # -- the rewriter --------------------------------------------------------
    def rewrite_ref(self, ref: str):
        r = self._split(ref)
        if r is None:
            return self._unresolved(ref)
        ctx, segs = r
        if len(segs) == 3 and segs[0] == "components" and segs[1] in KINDS:
            new = ctx.import_component(segs[1], segs[2])
            if new is None:
                return self._unresolved(ref)
            return {"$ref": f"#/components/{segs[1]}/{new}"}
        node = self._walk(ctx.doc, segs)
        guard = (id(ctx), tuple(segs))
        if node is None or guard in self.imp.inline_stack:
            return self._unresolved(ref)
        self.imp.inline_stack.add(guard)
        try:
            return ctx.rewrite(copy.deepcopy(node))
        finally:
            self.imp.inline_stack.discard(guard)

    def rewrite(self, node):
        if isinstance(node, list):
            return [self.rewrite(x) for x in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str):
            return self.rewrite_ref(ref)
        out = {}
        for k, v in node.items():
            k = str(k)                      # YAML parses `200:` as an int key
            if k in DROP_KEYS:
                continue
            if k in OPAQUE_KEYS or k.startswith("x-"):
                out[k] = copy.deepcopy(v)
            elif k in NAME_MAPS and isinstance(v, dict):
                out[k] = {str(n): self.rewrite(c) for n, c in v.items()}
            elif k == "examples" and isinstance(v, dict):
                out[k] = {str(n): self.rewrite(c) for n, c in v.items()}
            else:
                out[k] = self.rewrite(v)
        return downgrade(out)

    # -- security ------------------------------------------------------------
    def _scheme(self, name: str) -> str:
        sch = ((self.doc.get("components") or {}).get("securitySchemes") or {}).get(name)
        if not isinstance(sch, dict):
            return AUTH_SCHEME
        t = sch.get("type")
        if t == "oauth2" or (t == "http" and str(sch.get("scheme", "")).lower() == "bearer"):
            return AUTH_SCHEME
        new = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        base = new
        i = 1
        while new in self.imp.security_schemes and self.imp.security_schemes[new] != sch:
            i += 1
            new = f"{base}_{i}"
        self.imp.security_schemes[new] = copy.deepcopy(sch)
        return new

    def security(self, op: dict):
        """Mapped `security` for an operation, or None to inherit the global default."""
        eff = op["security"] if "security" in op else self.doc.get("security")
        if eff is None:
            return None
        if eff == []:
            return []
        mapped = []
        for req in eff:
            if not isinstance(req, dict):
                continue
            if not req:
                mapped.append({})
                continue
            mapped.append({self._scheme(n): list(sc or []) for n, sc in sorted(req.items())})
        uniq = []
        for m in mapped:
            if m not in uniq:
                uniq.append(m)
        return uniq

    # -- operations ----------------------------------------------------------
    def build_op(self, op: dict, path_params, path: str = "") -> dict:
        body = {k: v for k, v in op.items()
                if k not in ("parameters", "security", "tags", "servers", "operationId",
                             "x-stoplight", "callbacks")}
        out = self.rewrite(body)
        out.pop("x-stoplight", None)
        params, order = {}, []
        for p in list(path_params or []) + list(op.get("parameters") or []):
            if not isinstance(p, dict):
                continue
            target = self.peek(p["$ref"]) if isinstance(p.get("$ref"), str) else p
            if not isinstance(target, dict) or "name" not in target:
                key = ("ref", canon(p))
            else:
                key = (target.get("in"), target["name"])
            if isinstance(p.get("$ref"), str) and isinstance(target, dict) \
                    and {"in", "name"} <= target.keys() and "/components/parameters/" not in p["$ref"]:
                # a parameter object filed under another component kind: inline it as a parameter
                owner = self._split(p["$ref"])[0]
                new = owner.rewrite(copy.deepcopy(target))
            else:
                new = self.rewrite(p)
            if "x-unresolved-ref" in new:
                continue
            if key not in params:
                order.append(key)
            params[key] = new
        for name in re.findall(r"\{([^}/]+)\}", path):
            if ("path", name) not in params:    # OpenAPI requires every {template} be declared
                params[("path", name)] = {"name": name, "in": "path", "required": True,
                                          "schema": {"type": "string"}, "x-synthesized": True}
                order.append(("path", name))
                self.imp.synthesized.append({"source": self.src.name, "path": path, "param": name})
        if order:
            out["parameters"] = [params[k] for k in order]
        tags = []
        for t in op.get("tags") or []:
            n = t.get("name") if isinstance(t, dict) else t
            if isinstance(n, str) and n not in tags:
                tags.append(n)
        if tags:
            out["tags"] = tags
        sec = self.security(op)
        if sec is not None and sec != [{AUTH_SCHEME: []}]:
            out["security"] = sec
        oid = op.get("operationId")
        if isinstance(oid, str) and oid.strip():
            out["operationId"] = oid.strip()
        return out


# ===========================================================================
# Prefix learning: where does each service live on the public gateway?
# ===========================================================================

def _s(v) -> str:
    return v.strip().lower() if isinstance(v, str) else ""


TRIVIAL_PATHS = {"/", "/{}"}     # a bare "/" or "/{id}" says nothing about which service owns it
MIN_STOPLIGHT_MATCHES = 2        # several non-trivial operations must agree before Stoplight counts


def _score(a: dict, b: dict) -> int:
    """How many NON-TRIVIAL operations two {(METHOD, normalised relative path): op}
    maps share. Bare `/` and `/{id}` never count: nearly every service has them."""
    return sum(1 for k in a if k[1] not in TRIVIAL_PATHS and k in b)


def _rel_map(src: Source) -> dict:
    return {(m.upper(), norm_path(p or "/")): op for p, m, op, _ in iter_ops(src.doc)}


def _stoplight_prefix_for(lrel: dict, sls: list[Source], sl_ops: dict):
    """(prefix, service name) of the Stoplight service these operations match, or
    (None, None). Needs MIN_STOPLIGHT_MATCHES non-trivial matches, and every
    best-scoring service must agree on one prefix."""
    scored = [(_score(lrel, sl_ops[s.key]), s) for s in sls]
    scored = [(n, s) for n, s in scored if n >= MIN_STOPLIGHT_MATCHES]
    if not scored:
        return None, None
    top = max(n for n, _ in scored)
    tops = [s for n, s in scored if n == top]
    prefixes = {s.prefix for s in tops if s.prefix is not None}
    if len(prefixes) != 1:
        return None, None
    pfx = next(iter(prefixes))
    return pfx, sorted(s.name for s in tops if s.prefix == pfx)[0]


def learn_prefixes(lives: list[Source], sls: list[Source]):
    """Gateway prefix per live service, plus a prefix per Stoplight service.

    The baseline spec is deliberately NOT an input: it is the thing under test,
    so using it as evidence would make the output depend on what it is compared
    with. Evidence is only the two live sources:

    - the live YAML's own `servers[0].url` is authoritative;
    - a Stoplight service corroborates it when several non-trivial operations
      (never a bare `/`) match and its prefix is the same -> `matched`;
    - if Stoplight matches strongly but names a DIFFERENT prefix, the live
      prefix is still used, flagged `conflict`, and reported for a human;
    - with no usable live prefix, a strong Stoplight match supplies one;
      otherwise the service is `unknown` and its operations stay unplaced."""
    sl_ops = {s.key: _rel_map(s) for s in sls}
    info: dict[str, dict] = {}
    for L in lives:
        lops = [(m, p or "/") for p, m, _, _ in iter_ops(L.doc)]
        sp, sname = _stoplight_prefix_for(_rel_map(L), sls, sl_ops)
        declared = L.prefix
        prefix, conf, ev, note = None, "unknown", [], ""
        if declared is not None and sp is not None and declared == sp:
            prefix, conf, ev = declared, "matched", ["live-servers", "stoplight"]
        elif declared is not None and sp is not None:
            prefix, conf, ev = declared, "conflict", ["live-servers"]
            note = (f"placed at the live prefix {declared!r}, but Stoplight "
                    f"{sname!r} matches several operations at {sp!r}; needs a human check")
        elif declared is not None:
            prefix, conf, ev = declared, "declared", ["live-servers"]
        elif sp is not None:
            prefix, conf, ev = sp, "matched", ["stoplight"]
        for n in L.notes:
            note = (note + "; " if note else "") + n
        info[L.key] = {"service": L.name, "origin": L.origin, "prefix": prefix, "confidence": conf,
                       "evidence": ev, "declared": declared, "stoplight": sp,
                       "stoplight_service": sname, "note": note, "operations": len(lops)}

    # Stoplight services whose description gave no prefix borrow it from the
    # live service they match (same strict rule).
    sl_pref: dict[str, tuple] = {}
    placed = [L for L in lives if info[L.key]["prefix"] is not None]
    for s in sls:
        if s.prefix is not None:
            sl_pref[s.key] = (s.prefix, s.prefix_src)
            continue
        scored = [(_score(_rel_map(L), sl_ops[s.key]), info[L.key]["prefix"]) for L in placed]
        scored = [(n, p) for n, p in scored if n >= MIN_STOPLIGHT_MATCHES]
        top = max((n for n, _ in scored), default=0)
        pfx = {p for n, p in scored if n == top}
        sl_pref[s.key] = (next(iter(pfx)), "matched-live") if len(pfx) == 1 else (None, "")
    return info, sl_pref


async def probe_prefixes(cfg, info: dict, lives: list[Source]) -> None:
    """Optional, read-only, unauthenticated GETs to confirm a prefix exists:
    401/403/405 (and 400) mean the route exists, 404 means it does not. Goes
    through the same host-checked fetch function as every other request."""
    import httpx
    base = cfg.base_url.rstrip("/")
    by_key = {s.key: s for s in lives}
    sem = asyncio.Semaphore(1)
    one_shot = SimpleNamespace(retries=1, timeout=cfg.timeout, allowed_hosts=cfg.allowed_hosts)
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=False) as client:
        for key in sorted(info):
            i = info[key]
            if i["prefix"] is None or i["confidence"] not in ("declared", "conflict"):
                continue
            ops = sorted((m != "get", p) for p, m, _, _ in iter_ops(by_key[key].doc))[:3]
            results = []
            for _, p in ops:
                route = re.sub(r"[^A-Za-z0-9/_.~-]", "0", re.sub(r"\{[^}]*\}", "0", join_path(i["prefix"], p or "/")))
                r, _err = await _get(client, sem, base + route, one_shot)
                results.append(r.status_code if r is not None else 0)
                await asyncio.sleep(PROBE_DELAY)
            i["probe"] = results
            if any(c in (400, 401, 403, 405) or 200 <= c < 300 for c in results):
                if i["confidence"] == "declared":
                    i["confidence"] = "probed"
                i["evidence"].append("probe")
            elif results and all(c == 404 for c in results):
                i["probe_failed"] = True
                i["note"] = (i["note"] + "; " if i["note"] else "") + "probe returned 404 for every sampled route"


# ===========================================================================
# Facts: a comparable, description-free view of an operation (drift + history)
# ===========================================================================

STRIP = {"description", "summary", "title", "example", "examples", "externalDocs", "deprecated"}
MAX_DEPTH = 14


def _resolve_local(doc: dict, ref: str):
    if not ref.startswith("#/"):
        return None
    node = doc
    for s in ref[2:].split("/"):
        s = unquote(s).replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and s in node:
            node = node[s]
        else:
            return None
    return node


def _deref(doc: dict, node, path: str, stack: tuple, enums: dict, reqs: dict, depth: int = 0):
    if isinstance(node, list):
        return [_deref(doc, x, f"{path}[{i}]", stack, enums, reqs, depth) for i, x in enumerate(node)]
    if not isinstance(node, dict):
        return node
    ref = node.get("$ref")
    if isinstance(ref, str):
        if ref in stack or depth > MAX_DEPTH:
            return {"$cycle": ref.rsplit("/", 1)[-1]}
        target = _resolve_local(doc, ref)
        if target is None:
            return {"$unresolved": ref}
        return _deref(doc, target, path, stack + (ref,), enums, reqs, depth + 1)
    out = {}
    for k, v in node.items():
        if k in STRIP or k.startswith("x-"):
            continue
        if k == "enum" and isinstance(v, list):
            enums[path] = sorted(v, key=canon)
        elif k == "required" and isinstance(v, list):
            reqs[path] = sorted(str(x) for x in v)
        elif k == "default":
            out[k] = v
        elif k in NAME_MAPS and isinstance(v, dict):
            out[k] = {n: _deref(doc, c, f"{path}/{k}/{n}", stack, enums, reqs, depth + 1)
                      for n, c in v.items()}
        else:
            out[k] = _deref(doc, v, f"{path}/{k}", stack, enums, reqs, depth + 1)
    return out


def _scheme_kind(doc: dict, name: str) -> str:
    sch = ((doc.get("components") or {}).get("securitySchemes") or {}).get(name) or {}
    if sch.get("type") == "oauth2" or (sch.get("type") == "http" and str(sch.get("scheme", "")).lower() == "bearer"):
        return "bearer"
    return name


def op_facts(doc: dict, op: dict, path_params=None) -> dict:
    """Parameters, enums, required-ness, request/response schemas and auth,
    with every $ref resolved against `doc` and prose stripped."""
    enums: dict = {}
    reqs: dict = {}
    facts: dict = {"parameters": {}, "request": {}, "responses": {}}

    def target(o):
        if isinstance(o, dict) and isinstance(o.get("$ref"), str):
            return _resolve_local(doc, o["$ref"]) or {}
        return o if isinstance(o, dict) else {}

    seen = {}
    for p in list(path_params or []) + list(op.get("parameters") or []):
        t = target(p)
        if "name" in t and not (t.get("in") == "header" and
                                str(t["name"]).lower() in ("accept", "content-type", "authorization")):
            seen[(str(t.get("in")), t["name"])] = t   # those three headers are ignored per the OAS spec
    for (loc, name), t in sorted(seen.items()):
        key = f"{loc}:{name}"
        facts["parameters"][key] = {
            "schema": _deref(doc, t.get("schema", {}), f"param/{key}", (), enums, reqs),
            **{k: t[k] for k in ("style", "explode") if k in t}}
        reqs[f"param/{key}"] = bool(t.get("required"))
    rb = target(op.get("requestBody"))
    if rb:
        reqs["request/body"] = bool(rb.get("required"))
        for mt, m in sorted((rb.get("content") or {}).items()):
            facts["request"][mt] = _deref(doc, (m or {}).get("schema", {}), f"request/{mt}", (), enums, reqs)
    for code, r in sorted((op.get("responses") or {}).items(), key=lambda kv: str(kv[0])):
        for mt, m in sorted((target(r).get("content") or {}).items()):
            facts["responses"][f"{code}:{mt}"] = _deref(
                doc, (m or {}).get("schema", {}), f"response/{code}/{mt}", (), enums, reqs)
    sec = op["security"] if "security" in op else doc.get("security")
    if not sec:
        facts["auth"] = "none"
    else:
        reqs_ = [r for r in sec if isinstance(r, dict) and r]
        # a malformed block (e.g. nested lists of scheme objects) still means "auth required"
        facts["auth"] = sorted(canon({_scheme_kind(doc, n): sorted(sc or []) for n, sc in r.items()})
                               for r in reqs_) or [canon({"bearer": []})]
    facts["enums"], facts["required"] = enums, reqs
    return facts


def _diff_paths(a, b, path="", out=None, limit=8):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(f"+{path}/{k}")
            elif k not in b:
                out.append(f"-{path}/{k}")
            else:
                _diff_paths(a[k], b[k], f"{path}/{k}", out, limit)
    elif a != b:
        out.append(f"~{path or '/'}")
    return out[:limit]


def _prop_paths(node, prefix: str, out: set, depth: int = 0) -> None:
    """Every property path in a (dereferenced) schema, e.g. `repoId`, `a.b`, `items[].id`."""
    if not isinstance(node, dict) or depth > 12:
        return
    props = node.get("properties")
    if isinstance(props, dict):
        for name, sub in props.items():
            p = f"{prefix}.{name}" if prefix else name
            out.add(p)
            _prop_paths(sub, p, out, depth + 1)
    if isinstance(node.get("items"), dict):
        _prop_paths(node["items"], prefix + "[]", out, depth + 1)
    for k in ("allOf", "anyOf", "oneOf"):
        for sub in node.get(k) or []:
            _prop_paths(sub, prefix, out, depth + 1)


def response_props(doc: dict, op: dict, pp=None) -> set:
    """Property paths in an operation's 2xx response bodies."""
    out: set = set()
    for key, schema in op_facts(doc, op, pp)["responses"].items():
        if key.startswith("2"):
            _prop_paths(schema, "", out)
    return out


def _schema_diff(a, b, path="", out=None):
    """Every difference between two schemas as (kind, path, old, new); kind is
    add / remove / change."""
    out = [] if out is None else out
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append(("add", f"{path}/{k}", None, b[k]))
            elif k not in b:
                out.append(("remove", f"{path}/{k}", a[k], None))
            else:
                _schema_diff(a[k], b[k], f"{path}/{k}", out)
    elif a != b:
        out.append(("change", path or "/", a, b))
    return out


def classify_change(old: dict, new: dict, changes: dict) -> list[str]:
    """Reasons a change would break an existing caller; empty means additive.

    Breaking: removed parameter, a parameter or request field that became
    required, a removed enum value on input, a type change, a removed field,
    a response field that is no longer guaranteed, or a change of auth."""
    why: list[str] = []
    ps = changes.get("parameters", {})
    for k in ps.get("removed", []):
        # an optional header nobody needs to send is not worth breaking a build over
        if not (k.startswith("header:") and not old["required"].get(f"param/{k}")):
            why.append(f"removed parameter {k}")
    for k in ps.get("modified", []):
        diffs = _schema_diff(old["parameters"][k], new["parameters"][k])
        if any(kind == "change" and p.endswith("/type") for kind, p, _, _ in diffs):
            why.append(f"type change on parameter {k}")
    for loc, v in changes.get("enums", {}).items():
        if v.get("removed") and loc.startswith(("param/", "request/")) and new["enums"].get(loc):
            why.append(f"removed enum value(s) {v['removed']} on {loc}")
    for loc, v in changes.get("required", {}).items():
        inbound = loc.startswith(("param/", "request/"))
        if not inbound and not loc.startswith("response/2"):
            continue                              # error-body shapes do not break callers
        if "added" in v or "removed" in v:
            if inbound and v.get("added"):
                why.append(f"new required field(s) {v['added']} on {loc}")
            if not inbound and v.get("removed"):
                why.append(f"response field(s) {v['removed']} no longer required on {loc}")
        elif inbound and not v.get("from") and v.get("to"):
            why.append(f"{loc} is now required")
        elif not inbound and v.get("from") and not v.get("to"):
            why.append(f"{loc} is no longer guaranteed")
    for cat, label in (("request", "request"), ("responses", "response")):
        for key in sorted(set(old[cat]) | set(new[cat])):
            if old[cat].get(key) == new[cat].get(key):
                continue
            if cat == "responses" and not key.startswith("2"):
                continue                          # only the success shape can break a caller
            if key not in new[cat]:
                why.append(f"removed {label} body {key}")
                continue
            for kind, p, was, now in _schema_diff(old[cat].get(key), new[cat][key]):
                if isinstance(was, dict) and "$unresolved" in was:
                    continue                      # the old side was itself broken
                if kind == "remove" and p.rsplit("/", 2)[-2:-1] == ["properties"]:
                    why.append(f"removed {label} field {key}{p}")      # a property, not a constraint
                elif kind == "change" and p.endswith("/type") and isinstance(was, str) and isinstance(now, str):
                    why.append(f"type change at {label} {key}{p}")
    if "auth" in changes:
        why.append("authentication changed")
    return why


def compare_facts(old: dict, new: dict) -> dict:
    """What differs between two op_facts() results (empty dict = same)."""
    changes: dict = {}
    po, pn = old["parameters"], new["parameters"]
    added, removed = sorted(set(pn) - set(po)), sorted(set(po) - set(pn))
    modified = sorted(k for k in set(po) & set(pn) if po[k] != pn[k])
    if added or removed or modified:
        changes["parameters"] = {k: v for k, v in
                                 (("added", added), ("removed", removed), ("modified", modified)) if v}
    gone = tuple(f"param/{k}" for k in added + removed)
    for cat in ("enums", "required"):
        o, n = old[cat], new[cat]
        d = {}
        for loc in sorted(set(o) | set(n)):
            if o.get(loc) == n.get(loc) or (gone and loc.startswith(gone)):
                continue
            ov, nv = o.get(loc), n.get(loc)
            if isinstance(ov, list) or isinstance(nv, list):
                ov, nv = ov or [], nv or []
                d[loc] = {"added": [x for x in nv if x not in ov], "removed": [x for x in ov if x not in nv]}
            else:
                d[loc] = {"from": ov, "to": nv}
        if d:
            changes[cat] = d
    for cat, name in (("request", "request_schema"), ("responses", "response_schemas")):
        o, n = old[cat], new[cat]
        d = {k: _diff_paths(o.get(k), n.get(k)) for k in sorted(set(o) | set(n)) if o.get(k) != n.get(k)}
        if d:
            changes[name] = d
    if old["auth"] != new["auth"]:
        changes["auth"] = {"from": old["auth"], "to": new["auth"]}
    return changes


# ===========================================================================
# Merge
# ===========================================================================

@dataclass
class OpRec:
    src: Source
    method: str
    rel: str            # service-relative path
    path: str           # public gateway path
    op: dict
    pp: list | None


def _collect(srcs, prefix_of):
    """Index every operation of `srcs` by (METHOD, normalised public path).
    Returns (table, unplaced, collisions)."""
    table: dict = {}
    unplaced: list = []
    collisions: list = []
    for s in srcs:
        pfx = prefix_of(s)
        for path, m, op, pp in iter_ops(s.doc):
            rel = path or "/"
            if pfx is None:
                unplaced.append({"service": s.name, "origin": s.origin, "method": m.upper(),
                                 "service_path": rel, "summary": op.get("summary") or ""})
                continue
            full = join_path(pfx, rel)
            nk = nkey(m, full)
            if nk in table:
                collisions.append({"key": okey(m, full), "kept": table[nk].src.name, "dropped": s.name})
                continue
            table[nk] = OpRec(s, m, rel, full, op, pp)
    return table, unplaced, collisions


def _copy_examples(a, b) -> None:
    if not isinstance(a, dict) or not isinstance(b, dict) or "$ref" in a or "$ref" in b:
        return
    for mt, m in (a.get("content") or {}).items():
        bm = (b.get("content") or {}).get(mt)
        if isinstance(m, dict) and isinstance(bm, dict) and "example" not in m and "examples" not in m:
            for k in ("example", "examples"):
                if k in bm:
                    m[k] = copy.deepcopy(bm[k])


def _enrich(op: dict, sop: dict) -> None:
    """Stoplight supplies prose and examples; schemas stay live's."""
    for k in ("summary", "description"):
        v = sop.get(k)
        if isinstance(v, str) and v.strip():
            op[k] = v
    sp = {(p.get("in"), p.get("name")): p for p in sop.get("parameters", [])
          if isinstance(p, dict) and "name" in p}
    for p in op.get("parameters", []):
        if isinstance(p, dict) and "name" in p and not p.get("description"):
            q = sp.get((p.get("in"), p["name"]))
            if q and q.get("description"):
                p["description"] = q["description"]
    _copy_examples(op.get("requestBody"), sop.get("requestBody"))
    for code, r in (op.get("responses") or {}).items():
        _copy_examples(r, (sop.get("responses") or {}).get(code))


def _walk_refs(node, acc: set) -> None:
    if isinstance(node, list):
        for x in node:
            _walk_refs(x, acc)
    elif isinstance(node, dict):
        r = node.get("$ref")
        if isinstance(r, str):
            acc.add(r)
        for k, v in node.items():
            if k == "$ref":
                continue
            if (k in NAME_MAPS or k == "examples") and isinstance(v, dict):
                for child in v.values():       # keys here are names/status codes, e.g. `default`
                    _walk_refs(child, acc)
            elif k not in OPAQUE_KEYS and not k.startswith("x-"):
                _walk_refs(v, acc)


def prune_components(doc: dict) -> None:
    """Drop components nothing reaches (leftovers from enrichment-only imports)."""
    comps = doc.get("components", {})
    used: set = set()
    frontier: set = set()
    _walk_refs(doc.get("paths"), frontier)
    while frontier:
        ref = frontier.pop()
        m = re.match(r"#/components/(\w+)/(.+)$", ref)
        if not m or (m.group(1), m.group(2)) in used:
            continue
        used.add((m.group(1), m.group(2)))
        node = (comps.get(m.group(1)) or {}).get(m.group(2))
        if node is not None:
            _walk_refs(node, frontier)
    for kind in KINDS:
        if kind in comps:
            comps[kind] = {n: v for n, v in comps[kind].items() if (kind, n) in used}
            if not comps[kind]:
                del comps[kind]


def dangling_refs(doc: dict) -> list[str]:
    refs: set = set()
    _walk_refs(doc, refs)
    return sorted(r for r in refs if _resolve_local(doc, r) is None)


def build_spec(lives, extras, sls, overlays, history, info, sl_pref, run_date, refs=()) -> dict:
    """Merge everything. Returns the spec plus the bookkeeping the manifest,
    drift report and history need."""
    imp = Importer()
    all_live = sorted(lives + extras, key=lambda s: s.key)
    sls = sorted(sls, key=lambda s: (s.name, s.key))
    ctx_of = {id(s): imp.ctx(s) for s in all_live + sls + overlays}
    for s in refs:                    # resolvable by cross-file $refs, contribute no operations
        imp.ctx(s)

    live_tab, live_unplaced, live_coll = _collect(all_live, lambda s: info[s.key]["prefix"])
    sl_tab, sl_unplaced, sl_coll = _collect(sls, lambda s: sl_pref[s.key][0])

    merged: dict = {}      # nk -> {"path", "method", "op", "source", "service", ...}
    # pass 1: everything the live tenant serves
    for nk in sorted(live_tab, key=lambda k: (live_tab[k].src.key, live_tab[k].path, k)):
        r = live_tab[nk]
        op = ctx_of[id(r.src)].build_op(r.op, r.pp, r.rel)
        inf = info[r.src.key]
        merged[nk] = {"path": r.path, "method": r.method, "op": op, "source": "live",
                      "service": r.src.name, "prefix": inf["prefix"], "conf": inf["confidence"]}
    # pass 2: Stoplight - enrich shared operations, keep Stoplight-only ones
    for nk in sorted(sl_tab, key=lambda k: (sl_tab[k].src.name, sl_tab[k].src.key, sl_tab[k].path, k)):
        r = sl_tab[nk]
        sop = ctx_of[id(r.src)].build_op(r.op, r.pp, r.rel)
        sid = (r.op.get("x-stoplight") or {}).get("id")
        if nk in merged:
            m = merged[nk]
            pool = {"components": imp.components}
            m["sl_dropped"] = sorted(response_props(pool, sop) - response_props(pool, m["op"]))
            _enrich(m["op"], sop)
            m["source"] = "both"
            if sid:
                m["op"]["x-stoplight-id"] = sid
        else:
            sop["x-live-missing"] = True
            if sid:
                sop["x-stoplight-id"] = sid
            merged[nk] = {"path": r.path, "method": r.method, "op": sop, "source": "stoplight",
                          "service": r.src.name, "prefix": sl_pref[r.src.key][0],
                          "conf": sl_pref[r.src.key][1]}
    # pass 3: overlay (hand-maintained operations) wins over both sources
    overlay_applied, overlay_covered = [], []
    for s in overlays:
        c = ctx_of[id(s)]
        for path, m, op, pp in iter_ops(s.doc):
            nk = nkey(m, path)
            new = c.build_op(op, pp, path)
            prior = new.pop("x-source", None)
            new["x-source"] = "overlay"
            if prior:
                new["x-overlay-source"] = prior
            if nk in merged and merged[nk]["source"] != "overlay" and new.get("x-overlay-keep") is not True:
                overlay_covered.append({"key": okey(m, path), "covered_by": merged[nk]["source"],
                                        "overlay_file": s.file})
            merged[nk] = {"path": path, "method": m, "op": new, "source": "overlay",
                          "service": s.name, "prefix": None, "conf": "overlay"}
            overlay_applied.append({"key": okey(m, path), "overlay_file": s.file})

    # provenance fields, operationIds, paths, tags
    paths: dict = {}
    ids: dict = {}
    for nk in sorted(merged, key=lambda k: (merged[k]["path"], k[0])):
        m = merged[nk]
        op = m["op"]
        if m["source"] != "overlay":
            op["x-source"] = m["source"]
            op["x-service"] = m["service"]
            op["x-gateway-prefix"] = m["prefix"]
            op["x-prefix-confidence"] = m["conf"]
        oid = op.get("operationId")
        if oid:
            if oid in ids:
                n = 2
                cand = f"{oid}_{m['method']}"
                while cand in ids:
                    cand = f"{oid}_{m['method']}_{n}"
                    n += 1
                oid = cand
            ids[oid] = nk
            op["operationId"] = oid
        paths.setdefault(m["path"], {})[m["method"]] = op

    comps = {k: v for k, v in imp.components.items() if v}
    comps["securitySchemes"] = {AUTH_SCHEME: {
        "type": "http", "scheme": "bearer", "bearerFormat": "JWT",
        "description": "JWT access token obtained via the Authentication API"},
        **imp.security_schemes}
    tags = sorted({t for p in paths.values() for o in p.values() for t in o.get("tags", [])})
    doc = {"openapi": "3.0.3",
           "info": {"title": "Checkmarx One API", "version": "current",
                    "description": "Checkmarx One REST API merged from the live per-tenant catalog "
                                   "(schemas) and the Stoplight API reference (descriptions, examples). "
                                   "Generated candidate - review the drift report before adopting.",
                    "x-last-synced": run_date},
           "servers": SERVERS, "security": [{AUTH_SCHEME: []}],
           "components": comps, "tags": [{"name": t} for t in tags], "paths": paths}
    repairs = repair_doc(doc)
    prune_components(doc)

    # live-schema signatures -> x-live-verified (stable until the live schema changes)
    sigs = {}
    for nk, m in merged.items():
        if m["source"] in ("live", "both"):
            sig = sha256(canon(op_facts(doc, m["op"])))
            sigs[nk] = sig
            prev = (history.get("ops") or {}).get(f"{nk[0]} {nk[1]}") or {}
            m["op"]["x-live-verified"] = prev["live_verified"] if prev.get("live_sig") == sig \
                and prev.get("live_verified") else run_date
    return {"doc": doc, "merged": merged, "live_sigs": sigs, "info": info, "sl_pref": sl_pref,
            "live_unplaced": live_unplaced, "sl_unplaced": sl_unplaced,
            "collisions": live_coll + sl_coll, "overlay_applied": overlay_applied,
            "overlay_covered": overlay_covered, "unresolved": imp.unresolved, "synthesized": imp.synthesized, "repairs": repairs,
            "live_count": len(live_tab), "sl_count": len(sl_tab)}


# ===========================================================================
# Repairs: upstream specs contain slips that make a 3.0 document invalid.
# Every change is logged in the manifest; nothing is dropped silently.
# ===========================================================================

SCHEMA_KEYWORDS = {
    "title", "multipleOf", "maximum", "exclusiveMaximum", "minimum", "exclusiveMinimum", "maxLength",
    "minLength", "pattern", "maxItems", "minItems", "uniqueItems", "maxProperties", "minProperties",
    "required", "enum", "type", "not", "allOf", "oneOf", "anyOf", "items", "properties",
    "additionalProperties", "description", "format", "default", "nullable", "discriminator",
    "readOnly", "writeOnly", "example", "externalDocs", "deprecated", "xml", "$ref"}
PARAM_KEYWORDS = {"name", "in", "description", "required", "deprecated", "allowEmptyValue", "style",
                  "explode", "allowReserved", "schema", "example", "examples", "content", "$ref"}
_PY_TYPES = {"string": str, "boolean": bool, "array": list, "object": dict}


def _value_ok(schema: dict, v) -> bool:
    t, enum = schema.get("type"), schema.get("enum")
    if v is None:
        return True
    if isinstance(enum, list) and v not in enum:
        return False
    if t == "integer":
        return (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, float) and v.is_integer())
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t in _PY_TYPES:
        return isinstance(v, _PY_TYPES[t])
    return True


def repair_doc(doc: dict) -> list[dict]:
    """Normalise known upstream slips; returns the list of repairs made."""
    log: list[dict] = []

    def note(where, what):
        log.append({"where": where, "repair": what})

    def schema(s, where, depth=0):
        if not isinstance(s, dict) or "$ref" in s or depth > 40:
            return
        for k in [k for k in s if not k.startswith("x-") and k not in SCHEMA_KEYWORDS]:
            note(where, f"dropped unsupported schema keyword {k!r}")
            del s[k]
        if isinstance(s.get("required"), bool):
            note(where, "dropped boolean `required` on a schema")
            del s["required"]
        for k in ("default", "example"):
            if k in s and not _value_ok(s, s[k]):
                note(where, f"dropped {k} {json.dumps(s[k])[:40]} that does not fit the schema")
                del s[k]
        for name, sub in (s.get("properties") or {}).items():
            schema(sub, f"{where}/properties/{name}", depth + 1)
        items = s.get("items")
        if isinstance(items, dict):
            schema(items, f"{where}/items", depth + 1)
        if isinstance(s.get("additionalProperties"), dict):
            schema(s["additionalProperties"], f"{where}/additionalProperties", depth + 1)
        for k in ("allOf", "anyOf", "oneOf"):
            for i, sub in enumerate(s.get(k) or []):
                schema(sub, f"{where}/{k}/{i}", depth + 1)
        if isinstance(s.get("not"), dict):
            schema(s["not"], f"{where}/not", depth + 1)

    def content(c, where):
        for mt, m in (c or {}).items():
            if isinstance(m, dict) and isinstance(m.get("schema"), dict):
                schema(m["schema"], f"{where}/{mt}")
            for name, ex in ((m or {}).get("examples") or {}).items() if isinstance(m, dict) else ():
                if not isinstance(ex, dict) or "$ref" in ex:
                    continue
                if "title" in ex:
                    note(f"{where}/{mt}/examples/{name}", "renamed example `title` to `summary`")
                    ex.setdefault("summary", ex.pop("title"))
                    ex.pop("title", None)
                for k in [k for k in ex if not k.startswith("x-")
                          and k not in ("summary", "description", "value", "externalValue")]:
                    note(f"{where}/{mt}/examples/{name}", f"dropped unsupported example keyword {k!r}")
                    del ex[k]

    def param(p, where):
        if "$ref" in p:
            return
        if "schema" not in p and "content" not in p:
            moved = {k: p.pop(k) for k in list(p) if k in SCHEMA_KEYWORDS - PARAM_KEYWORDS}
            if moved:
                note(where, f"moved {sorted(moved)} into `schema`")
            else:
                note(where, "added an empty `schema` (parameter had none)")
            p["schema"] = moved
        for k in [k for k in p if not k.startswith("x-") and k not in PARAM_KEYWORDS]:
            if k == "default" and isinstance(p.get("schema"), dict) and "default" not in p["schema"]:
                p["schema"]["default"] = p.pop(k)
                note(where, "moved parameter-level `default` into `schema`")
            else:
                note(where, f"dropped unsupported parameter keyword {k!r}")
                del p[k]
        if isinstance(p.get("schema"), dict):
            schema(p["schema"], where + "/schema")

    for path, item in doc.get("paths", {}).items():
        for m, op in item.items():
            where = f"{m.upper()} {path}"
            if op.get("requestBody", 0) is None:
                del op["requestBody"]
                note(where, "dropped null requestBody")
            params = []
            for p in op.get("parameters", []):
                if isinstance(p, dict) and p.get("in") == "path" and "name" in p \
                        and "{" + p["name"] + "}" not in path:
                    note(where, f"dropped path parameter {p['name']!r} that is not in the URL template")
                    continue
                param(p, f"{where} param {p.get('name', p.get('$ref', '?'))}")
                params.append(p)
            if "parameters" in op:
                op["parameters"] = params
            rb = op.get("requestBody")
            if isinstance(rb, dict) and "$ref" not in rb:
                content(rb.get("content"), where + " requestBody")
            for code, r in (op.get("responses") or {}).items():
                if isinstance(r, dict) and "$ref" not in r:
                    content(r.get("content"), f"{where} response {code}")
    comps = doc.get("components", {})
    for name, s in comps.get("schemas", {}).items():
        schema(s, f"components/schemas/{name}")
    for name, p in comps.get("parameters", {}).items():
        if isinstance(p, dict):
            param(p, f"components/parameters/{name}")
    for name, r in comps.get("responses", {}).items():
        if isinstance(r, dict):
            content(r.get("content"), f"components/responses/{name}")
    for name, r in comps.get("requestBodies", {}).items():
        if isinstance(r, dict):
            content(r.get("content"), f"components/requestBodies/{name}")
    return log


# ===========================================================================
# History + drift
# ===========================================================================

def update_history(old: dict, live_sigs: dict, run_date: str) -> dict:
    ops = {k: dict(v) for k, v in (old.get("ops") or {}).items()}
    present = {f"{nk[0]} {nk[1]}": sig for nk, sig in live_sigs.items()}
    for k, sig in present.items():
        rec = ops.get(k, {})
        same = rec.get("live_sig") == sig and rec.get("live_verified")
        ops[k] = {"live_sig": sig, "live_verified": rec["live_verified"] if same else run_date,
                  "first_seen": rec.get("first_seen", run_date), "last_seen": run_date,
                  "live_seen": True, "missing_runs": 0}
    for k, rec in ops.items():
        if k not in present and rec.get("live_seen"):
            rec["missing_runs"] = rec.get("missing_runs", 0) + 1
    runs = [r for r in (old.get("runs") or []) if r != run_date] + [run_date]
    return {"version": HISTORY_VERSION, "runs": runs[-52:], "ops": ops}


def _index_doc(doc: dict) -> dict:
    return {nkey(m, path): (path, m, op, pp) for path, m, op, pp in iter_ops(doc)}


def abs_path(p) -> Path:
    """Normalise an operator-supplied path before any file access."""
    return Path(os.path.abspath(os.path.normpath(os.path.expanduser(str(p)))))


def read_input_text(path, what: str) -> str:
    """Read an operator-supplied file, after normalising it to a real regular file."""
    p = abs_path(path)
    if not p.is_file():
        raise FatalError(f"{what} not found or not a file: {path}")
    return p.read_text(encoding="utf-8")


def load_used(path: str | None) -> list[tuple]:
    """Endpoints the consumer depends on. Accepts a JSON list/object or a
    text file; entries are 'METHOD /path' or just '/path' (any method)."""
    if not path:
        return []
    text = read_input_text(path, "--used-endpoints file")
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            data = data.get("endpoints") or data.get("used") or list(data)
        items = [x if isinstance(x, str) else f"{x.get('method', '')} {x.get('path', '')}" for x in data]
    except ValueError:
        items = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    used = []
    verbs = {m.upper() for m in METHODS}
    for it in items:
        parts = it.strip().split(None, 1)
        if len(parts) == 2 and parts[0].upper() in verbs:
            used.append((parts[0].upper(), norm_path(urlparse(parts[1]).path or parts[1])))
        elif parts:
            used.append((None, norm_path(urlparse(parts[-1]).path or parts[-1])))
    return used


def shrink_check(res: dict, baseline_ops: int | None, used: list) -> list[str]:
    """Reasons the merged spec looks like a partial fetch (empty = fine)."""
    if baseline_ops is None:
        return []
    why = []
    n = len(res["merged"])
    if baseline_ops and (baseline_ops - n) / baseline_ops > SHRINK_LIMIT:
        why.append(f"the merged spec has {n} operations, {100 * (baseline_ops - n) / baseline_ops:.1f}% "
                   f"fewer than the baseline's {baseline_ops} (limit {int(SHRINK_LIMIT * 100)}%)")
    keys = set(res["merged"])
    missing = sorted(f"{m or '*'} {p}" for m, p in used
                     if not any(np == p and m in (None, nm) for nm, np in keys))
    if missing:
        why.append(f"{len(missing)} --used-endpoints entr{'y is' if len(missing) == 1 else 'ies are'} "
                   f"missing from the merged spec: " + ", ".join(missing[:10]) + (" ..." if len(missing) > 10 else ""))
    return why


def compute_drift(res: dict, baseline: dict | None, new_hist: dict, used: list) -> dict:
    doc, merged = res["doc"], res["merged"]
    bidx = _index_doc(baseline) if baseline else {}
    drift: dict = {"added": [], "changed": [], "deprecated": [], "removed_candidate": [],
                   "baseline_only": [], "prefix_unknown": [], "prefix_conflict": [],
                   "stoplight_only": [], "response_fields_dropped": []}

    def rec(nk, extra=None):
        m = merged[nk]
        r = {"key": okey(m["method"], m["path"]), "service": m["service"], "source": m["source"],
             "summary": m["op"].get("summary") or ""}
        r.update(extra or {})
        return r

    for nk in sorted(merged):
        m = merged[nk]
        op = m["op"]
        base = bidx.get(nk)
        if base is None:
            if baseline and m["source"] != "overlay":
                drift["added"].append(rec(nk))
        elif m["source"] != "overlay":
            of, nf = op_facts(baseline, base[2], base[3]), op_facts(doc, op)
            ch = compare_facts(of, nf)
            if ch:
                why = classify_change(of, nf, ch)
                drift["changed"].append(rec(nk, {
                    "baseline_path": base[0], "changes": ch,
                    "severity": "breaking" if why else "additive", "breaking_reasons": why[:10]}))
        if op.get("deprecated") is True:
            newly = bool(base) and base[2].get("deprecated") is not True
            drift["deprecated"].append(rec(nk, {"newly_deprecated": newly}))
        if m["source"] == "stoplight":
            drift["stoplight_only"].append(rec(nk))
        if m["conf"] == "conflict":
            drift["prefix_conflict"].append(rec(nk))
        # informational: response properties a reference source has and the merged operation lacks
        from_base = sorted(response_props(baseline, base[2], base[3]) - response_props(doc, op)) \
            if base is not None else []
        from_sl = m.get("sl_dropped", [])
        if from_base or from_sl:
            drift["response_fields_dropped"].append(rec(nk, {
                "properties": sorted(set(from_base) | set(from_sl)),
                "in_baseline": from_base, "in_stoplight": from_sl}))
    for nk in sorted(bidx):
        if nk not in merged:
            path, meth, op, _ = bidx[nk]
            drift["baseline_only"].append({"key": okey(meth, path), "summary": op.get("summary") or "",
                                           "x-source": op.get("x-source")})
    for k, h in sorted((new_hist.get("ops") or {}).items()):
        if h.get("live_seen") and h.get("missing_runs", 0) >= REMOVED_AFTER_RUNS:
            drift["removed_candidate"].append({"key": k, "missing_runs": h["missing_runs"],
                                               "last_seen": h.get("last_seen")})
    for u in res["live_unplaced"]:
        drift["prefix_unknown"].append({"key": f"{u['method']} <{u['service']}>{u['service_path']}",
                                        "service": u["service"], "summary": u["summary"]})
    drift["overlay"] = {"applied": res["overlay_applied"], "covered_by_source": res["overlay_covered"]}

    hits, dep = [], []
    if used:
        def is_used(key: str) -> bool:
            meth, path = key.split(" ", 1)
            return any(um in (None, meth) and up == norm_path(path) for um, up in used)
        for cat in ("changed", "removed_candidate", "baseline_only"):
            for r in drift[cat]:
                if is_used(r["key"]):
                    hits.append({"key": r["key"], "category": cat,
                                 "severity": r.get("severity", "breaking")})
        dep = [r["key"] for r in drift["deprecated"] if is_used(r["key"])]
    drift["used_endpoints"] = {
        "checked": len(used), "affected": hits, "deprecated_in_use": dep,
        "breaking": [h for h in hits if h["severity"] == "breaking"],
        "additive": [h for h in hits if h["severity"] != "breaking"]}
    drift["summary"] = {k: len(v) for k, v in drift.items() if isinstance(v, list)}
    drift["summary"].update(
        changed_breaking=sum(1 for r in drift["changed"] if r["severity"] == "breaking"),
        changed_additive=sum(1 for r in drift["changed"] if r["severity"] != "breaking"),
        overlay_applied=len(res["overlay_applied"]), overlay_now_covered=len(res["overlay_covered"]),
        used_endpoints_breaking=len(drift["used_endpoints"]["breaking"]),
        used_endpoints_additive=len(drift["used_endpoints"]["additive"]))
    drift["baseline_operations"] = len(bidx) if baseline else None
    return drift


# ===========================================================================
# Reports
# ===========================================================================

def _table(rows: list[list], head: list[str], cap: int = 25) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for r in rows[:cap]:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")
    if len(rows) > cap:
        out.append(f"| ... {len(rows) - cap} more (see api-spec-drift.json) |" + " |" * (len(head) - 1))
    return out


RISK = {"auth": 5, "required": 4, "request_schema": 4, "parameters": 3, "enums": 3, "response_schemas": 1}


def _risk(ch: dict) -> int:
    return sum(RISK.get(k, 1) for k in ch)


def _brief_changes(ch: dict) -> str:
    bits = []
    for cat, v in ch.items():
        if cat in ("enums", "required", "request_schema", "response_schemas"):
            bits.append(f"{cat}({len(v)})")
        elif cat == "parameters":
            bits.append("parameters(" + ", ".join(f"{k} {len(x)}" for k, x in v.items()) + ")")
        else:
            bits.append(cat)
    return ", ".join(bits)


def write_report(path: Path, drift: dict, manifest: dict, run_date: str,
                 baseline_label: str | None, notes: list[str]) -> None:
    s = drift["summary"]
    c = manifest["counts"]
    src = manifest["sources"]
    L = [f"# Checkmarx One API spec - sync report ({run_date})", "",
         f"- **Merged spec:** {c['paths']} paths, {c['operations']} operations "
         f"(live {c['by_source'].get('live', 0)}, both {c['by_source'].get('both', 0)}, "
         f"Stoplight-only {c['by_source'].get('stoplight', 0)}, overlay {c['by_source'].get('overlay', 0)}).",
         f"- **Sources:** live catalog {src['live']['base_url']} "
         f"({src['live']['services']} services, {src['live']['operations']} ops); "
         f"Stoplight ({src['stoplight']['services']} services, {src['stoplight']['operations']} ops).",
         f"- **Baseline:** {baseline_label or 'none given - baseline-dependent categories are empty'}"
         + (f" ({drift['baseline_operations']} ops)." if drift["baseline_operations"] is not None else "."),
         f"- **Validation:** {manifest['validation']['summary']}", ""]
    g = drift.get("shrink_guard") or {}
    if g.get("fired"):
        L += ["## SHRINK GUARD FIRED" + (" (overridden by --allow-shrink)" if g["overridden"]
                                          else " - cxone_openapi.json was NOT written"), "",
              "This looks like a partial fetch. Check the fetch problems at the bottom before trusting the numbers.", ""]
        L += [f"- {w}" for w in g["fired"]] + [""]
    L += ["## Needs attention", ""]
    ue = drift["used_endpoints"]
    if ue["checked"]:
        L += [f"### Endpoints you use with BREAKING drift ({len(ue['breaking'])} operations; "
              f"{ue['checked']} entries checked)", "",
              "These make the run exit with code 10.", ""]
        L += _table([[a["key"], a["category"]] for a in ue["breaking"]], ["Endpoint", "Drift"]) \
            if ue["breaking"] else ["None."]
        L += ["", f"Additive drift on endpoints you use ({len(ue['additive'])}, informational): "
              + (", ".join(f"`{a['key']}`" for a in ue["additive"][:15])
                 + (" ..." if len(ue["additive"]) > 15 else "") if ue["additive"] else "none.")]
        if ue["deprecated_in_use"]:
            L += ["", "Deprecated but in use: " + ", ".join(f"`{k}`" for k in ue["deprecated_in_use"])]
        L.append("")
    L += [f"### Removed candidates ({s['removed_candidate']})", "",
          f"Seen live before, then absent from live for {REMOVED_AFTER_RUNS}+ runs in a row.", ""]
    L += _table([[r["key"], r["missing_runs"], r["last_seen"]] for r in drift["removed_candidate"]],
                ["Operation", "Runs missing", "Last seen"]) if drift["removed_candidate"] else ["None."]
    L += ["", f"### In the baseline but not in this merge ({s['baseline_only']})", "",
          "No source and no overlay entry provides these. Add them to the overlay or confirm they are gone.", ""]
    L += _table([[r["key"], r["x-source"] or ""] for r in drift["baseline_only"]],
                ["Operation", "Baseline x-source"]) if drift["baseline_only"] else ["None."]
    L += ["", f"### Changed ({s['changed']}: {s['changed_breaking']} breaking, "
              f"{s['changed_additive']} additive) - breaking first", ""]
    ranked = sorted(drift["changed"], key=lambda r: (r["severity"] != "breaking", -_risk(r["changes"]), r["key"]))
    L += _table([[r["key"], r["severity"], _brief_changes(r["changes"])] for r in ranked],
                ["Operation", "Severity", "What changed"]) if drift["changed"] else ["None."]
    L += ["", f"### Deprecated ({s['deprecated']})", ""]
    L += _table([[r["key"], "new" if r["newly_deprecated"] else ""] for r in drift["deprecated"]],
                ["Operation", "Newly deprecated"]) if drift["deprecated"] else ["None."]
    bad = {k: v for k, v in manifest["prefix_map"].items()
           if v["confidence"] in ("conflict", "unknown") or v.get("probe_failed")}
    L += ["", f"### Prefix problems ({len(bad)} services; {s['prefix_unknown']} operations not placed, "
              f"{s['prefix_conflict']} placed at a disputed prefix)", ""]
    L += _table([[k, "not routable" if v.get("probe_failed") else v["confidence"], v["note"] or ""]
                 for k, v in sorted(bad.items())],
                ["Service", "State", "Note"]) if bad else ["None."]
    if drift["overlay"]["covered_by_source"]:
        L += ["", f"### Overlay entries a source now covers ({s['overlay_now_covered']}) - retire candidates", ""]
        L += _table([[o["key"], o["covered_by"], o["overlay_file"]] for o in drift["overlay"]["covered_by_source"]],
                    ["Operation", "Now in", "Overlay file"])

    L += ["", "## Informational", "", f"### Added ({s['added']})", ""]
    by_svc: dict = {}
    for r in drift["added"]:
        by_svc.setdefault(r["service"], []).append(r["key"])
    L += _table([[svc, len(keys), ", ".join(keys[:3]) + (" ..." if len(keys) > 3 else "")]
                 for svc, keys in sorted(by_svc.items())],
                ["Service", "New ops", "Examples"], cap=40) if by_svc else ["None."]
    L += ["", f"### Response fields dropped ({s['response_fields_dropped']}) - check before adopting", "",
          "Properties the baseline or Stoplight returns that the live schema does not list. Live schemas can "
          "omit fields that really exist (for example `repoId` on `GET /api/projects/{id}`). Informational; "
          "never fails a run.", ""]
    L += _table([[r["key"], ", ".join(r["properties"][:8]) + (" ..." if len(r["properties"]) > 8 else ""),
                  "baseline" if r["in_baseline"] and not r["in_stoplight"] else
                  "Stoplight" if r["in_stoplight"] and not r["in_baseline"] else "both"]
                 for r in drift["response_fields_dropped"]],
                ["Operation", "Properties", "Seen in"]) if drift["response_fields_dropped"] else ["None."]
    L += ["", f"### Stoplight-only ({s['stoplight_only']}) - not served by the live catalog", ""]
    L += _table([[r["key"], r["service"]] for r in drift["stoplight_only"]],
                ["Operation", "Stoplight service"]) if drift["stoplight_only"] else ["None."]
    items = list(notes) + [f"{x['source']}: unresolved $ref {x['ref']}" for x in manifest["unresolved_refs"][:10]]
    items += [f"Stoplight service not placed (no gateway prefix): {u}"
              for u in manifest["problems"]["stoplight_unplaced_services"]]
    L += ["", "### Fetch and merge problems", ""]
    L += [f"- {i}" for i in items] if items else ["None."]
    L.append("")
    write_atomic(path, "\n".join(L))


def _load_validator():
    try:
        from openapi_spec_validator import OpenAPIV30SpecValidator
    except ImportError:
        raise FatalError("openapi-spec-validator is required to validate the output: "
                         "python -m pip install -r requirements.txt  (or pass --skip-validation)") from None
    return OpenAPIV30SpecValidator


def validate_openapi(doc: dict, skip: bool = False) -> dict:
    """Dangling-$ref check plus the OpenAPI 3.0 validator (skipped with --skip-validation)."""
    out = {"dangling_refs": dangling_refs(doc)}
    if skip:
        out["validator"] = "skipped"
    else:
        validator = _load_validator()
        try:
            errs = [e.message[:200] for e in validator(doc).iter_errors()]
        except Exception as e:   # the validator raises, rather than yields, on badly malformed input
            errs = [f"validator could not finish: {type(e).__name__}: {str(e)[:150]}"]
        out["validator_errors"] = errs[:20]
        out["validator_error_count"] = len(errs)
        out["validator"] = "ok" if not errs else "errors"
    refs = "0 dangling $refs" if not out["dangling_refs"] else f"{len(out['dangling_refs'])} DANGLING $refs"
    verdict = {"ok": "passes the OpenAPI 3.0 validator.",
               "errors": f"{out.get('validator_error_count')} OpenAPI validator errors (see manifest).",
               "skipped": "OpenAPI validator skipped (--skip-validation)."}
    out["summary"] = f"{refs}; " + verdict[out["validator"]]
    return out


def build_manifest(res: dict, raw: RawSet, drift: dict, validation: dict, cfg, run_date: str,
                   lives, extras, sls) -> dict:
    merged = res["merged"]
    by_source: dict = {}
    for m in merged.values():
        by_source[m["source"]] = by_source.get(m["source"], 0) + 1
    branches = (raw.json("stoplight/branches.json") or {}).get("items", [])
    main = next((b for b in branches if b.get("is_default")), branches[0] if branches else {})
    not_ok = {rel: {k: e.get(k) for k in ("status", "error")}
              for rel, e in sorted(raw.entries.items()) if e.get("status") != "ok"}
    return {
        "run_date": run_date,
        "counts": {"paths": len(res["doc"]["paths"]), "operations": len(merged), "by_source": by_source,
                   "components": {k: len(v) for k, v in res["doc"]["components"].items()}},
        "sources": {
            "live": {"base_url": cfg.base_url, "services": len(lives), "extra_services": len(extras),
                     "operations": sum(1 for s in lives + extras for _ in iter_ops(s.doc))},
            "stoplight": {"host": STOPLIGHT_HOST, "project": STOPLIGHT_PROJECT_SLUG,
                          "branch": main.get("slug"), "commit": main.get("commit_hash"),
                          "node_counts": main.get("node_counts"), "services": len(sls),
                          "operations": sum(1 for s in sls for _ in iter_ops(s.doc))}},
        "prefix_map": {k: res["info"][k] for k in sorted(res["info"])},
        "stoplight_prefixes": {f"{s.name} [{s.key}]": {"prefix": res["sl_pref"][s.key][0],
                                                      "source": res["sl_pref"][s.key][1], "notes": s.notes}
                               for s in sorted(sls, key=lambda x: (x.name, x.key))},
        "unplaced_live_operations": res["live_unplaced"],
        "unplaced_stoplight_operations": res["sl_unplaced"],
        "collisions": res["collisions"],
        "unresolved_refs": res["unresolved"],
        "synthesized_path_params": res["synthesized"],
        "repairs": {"count": len(res["repairs"]), "details": res["repairs"][:200]},
        "problems": {"stoplight_unplaced_services": sorted({u["service"] for u in res["sl_unplaced"]}),
                     "files_not_ok": not_ok},
        "validation": validation,
        "drift_summary": drift["summary"],
    }


# ===========================================================================
# Orchestration + CLI
# ===========================================================================

def _read_doc(path: Path):
    text = read_input_text(path, "spec file")
    return json.loads(text) if path.suffix.lower() == ".json" else _strkeys(_yaml().safe_load(text))


def load_overlay(overlay_dir: str | None) -> list[Source]:
    out = []
    d = abs_path(overlay_dir) if overlay_dir else None
    if d and d.is_dir():
        for f in sorted(p for p in d.iterdir() if p.suffix.lower() in (".yaml", ".yml", ".json")):
            doc = _read_doc(f)
            if isinstance(doc, dict) and isinstance(doc.get("paths"), dict):
                out.append(Source("overlay", f.stem, f"overlay:{f.name}", f.name, f.name, doc))
    return out


def run(cfg) -> int:
    try:
        return _run(cfg)
    except FatalError as e:
        print(f"[api-spec] FATAL: {e}", file=sys.stderr)
        return EXIT_FATAL


def _run(cfg) -> int:
    if not cfg.skip_validation:
        _load_validator()          # fail before fetching anything if it is missing
    if cfg.auth_token_env and not cfg.from_raw:
        cfg.auth_token = os.environ.get(cfg.auth_token_env, "").strip()
        if not cfg.auth_token:
            raise FatalError(f"--auth-token-env {cfg.auth_token_env}: that environment variable is not set or empty")
    out = abs_path(cfg.out_dir)
    sweep_tmp(out)
    raw_dir = out / "raw"
    notes: list[str] = []
    fetched = False
    if cfg.from_raw:
        raw = RawSet.load(abs_path(cfg.from_raw))
        if not raw.blobs:
            raise FatalError(f"nothing under {cfg.from_raw}")
        print(f"[api-spec] rebuilding from {cfg.from_raw} (no network)")
    else:
        prev = RawSet.load(raw_dir) if raw_dir.is_dir() else None
        print(f"[api-spec] fetching live catalog from {cfg.base_url} and Stoplight "
              f"(<= {min(cfg.concurrency, API_CONCURRENCY)} concurrent)")
        raw, fnotes = asyncio.run(fetch_all(cfg, prev))
        notes += fnotes
        fetched = True
        if not cfg.dry_run:
            raw.save(raw_dir)

    lives, extras, sls, refs = load_sources(raw, notes)
    if not lives or not sls:
        raise FatalError("no live or Stoplight specs could be loaded")
    overlays = load_overlay(cfg.overlay_dir)
    baseline = _read_doc(abs_path(cfg.baseline)) if cfg.baseline else None
    hist_path = out / "history.json"
    history = json.loads(read_input_text(hist_path, "history")) if hist_path.is_file() else {}
    if history and history.get("version") != HISTORY_VERSION:
        notes.append("history.json was written before the placement fix and is ignored; "
                     "a fresh history starts with this run")
        history = {}
    run_date = cfg.run_date or today()

    info, sl_pref = learn_prefixes(lives + extras, sls)
    if cfg.probe:
        print("[api-spec] probing declared prefixes (read-only, unauthenticated)")
        asyncio.run(probe_prefixes(cfg, info, lives + extras))
    res = build_spec(lives, extras, sls, overlays, history, info, sl_pref, run_date, refs)
    res["doc"]["info"]["x-sync-notes"] = (
        f"cx_docs_mirror api-spec stage. Live: {len(lives)} services from {cfg.base_url}; "
        f"Stoplight: {len(sls)} services; overlay files: {len(overlays)}. "
        "Schemas, parameters, enums and required-ness come from live; prose and examples from Stoplight.")
    new_hist = update_history(history, res["live_sigs"], run_date)
    used = load_used(cfg.used_endpoints)
    drift = compute_drift(res, baseline, new_hist, used)
    fired = shrink_check(res, drift["baseline_operations"], used)
    blocked = bool(fired) and not cfg.allow_shrink
    drift["shrink_guard"] = {"checked": baseline is not None, "limit": SHRINK_LIMIT, "fired": fired,
                             "overridden": bool(fired) and cfg.allow_shrink, "spec_written": not blocked}
    validation = validate_openapi(res["doc"], cfg.skip_validation)
    manifest = build_manifest(res, raw, drift, validation, cfg, run_date, lives, extras, sls)
    drift = {"run_date": run_date, "baseline": cfg.baseline, **drift}

    report = out / "API-SPEC-REPORT.md"
    write_report(report, drift, manifest, run_date, cfg.baseline, notes)
    if not cfg.dry_run:
        if not blocked:
            dump_json(out / "cxone_openapi.json", res["doc"])
        dump_json(out / "api-spec-manifest.json", manifest)
        dump_json(out / "api-spec-drift.json", drift)
        if fetched and not blocked:   # a rebuild from saved raw files, or a blocked run, is not a new run
            dump_json(hist_path, new_hist)

    c = manifest["counts"]
    print(f"[api-spec] {c['paths']} paths / {c['operations']} ops {c['by_source']}; {validation['summary']}")
    print("[api-spec] drift: " + (", ".join(f"{k} {v}" for k, v in drift["summary"].items() if v) or "none"))
    for n in notes:
        print(f"[api-spec] note: {n}")
    print(f"[api-spec] report: {report}" + (" (dry run: nothing else written)" if cfg.dry_run else ""))
    if blocked:
        print("[api-spec] SHRINK GUARD fired; cxone_openapi.json was NOT written "
              "(inspect the report, or pass --allow-shrink):", file=sys.stderr)
        for w in fired:
            print(f"[api-spec]   - {w}", file=sys.stderr)
        return EXIT_SHRINK
    return EXIT_USED_DRIFT if drift["used_endpoints"]["breaking"] else 0


def add_arguments(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group("api-spec stage")
    g.add_argument("--api-out", help=f"Output directory (default {API_OUT_DIR})")
    g.add_argument("--api-base-url", help=f"Tenant root for the live catalog (default {API_BASE_URL})")
    g.add_argument("--api-extra-services", nargs="*",
                   help="Services missing from the catalog that serve {base}/api/<name>/openapi.json")
    g.add_argument("--baseline", help="Current spec to diff against (never modified)")
    g.add_argument("--overlay", help=f"Directory of hand-maintained operations (default {API_OVERLAY_DIR}/ if present)")
    g.add_argument("--used-endpoints", help="File of endpoints your code calls; drift on them exits non-zero")
    g.add_argument("--dry-run", action="store_true", help="Fetch and report; write only API-SPEC-REPORT.md")
    g.add_argument("--from-raw", metavar="DIR", help="Rebuild from an earlier raw/ directory, no network")
    g.add_argument("--probe", action="store_true",
                   help="Confirm declared prefixes with a few unauthenticated GETs (off by default)")
    g.add_argument("--allow-shrink", action="store_true",
                   help="With --baseline: write the spec even if the shrink guard fires "
                        "(>5%% fewer operations than the baseline, or a --used-endpoints entry missing)")
    g.add_argument("--auth-token-env", metavar="VAR",
                   help="Name of an environment variable holding a tenant bearer token. Opt-in: "
                        "used only to fetch the extra services that need a login; never stored or printed")
    g.add_argument("--run-date", help="Pin the sync date (YYYY-MM-DD), e.g. to reproduce a run")
    g.add_argument("--skip-validation", action="store_true",
                   help="Do not run the OpenAPI 3.0 validator on the merged spec (it runs by default)")


def build_cfg(args: argparse.Namespace) -> SimpleNamespace:
    base = (getattr(args, "api_base_url", None) or API_BASE_URL).rstrip("/")
    overlay = getattr(args, "overlay", None)
    if overlay is None and Path(API_OVERLAY_DIR).is_dir():
        overlay = API_OVERLAY_DIR
    extra = getattr(args, "api_extra_services", None)
    return SimpleNamespace(
        base_url=base,
        extra_services=API_EXTRA_SERVICES if extra is None else extra,
        out_dir=getattr(args, "api_out", None) or API_OUT_DIR,
        overlay_dir=overlay, baseline=getattr(args, "baseline", None),
        used_endpoints=getattr(args, "used_endpoints", None),
        dry_run=getattr(args, "dry_run", False), from_raw=getattr(args, "from_raw", None),
        probe=getattr(args, "probe", False), run_date=getattr(args, "run_date", None),
        skip_validation=getattr(args, "skip_validation", False),
        auth_token_env=getattr(args, "auth_token_env", None), auth_token=None,
        allow_shrink=getattr(args, "allow_shrink", False),
        concurrency=API_CONCURRENCY, timeout=API_TIMEOUT, retries=API_RETRIES,
        allowed_hosts={(urlparse(base).hostname or "").lower(), "stoplight.io", "checkmarx.stoplight.io"})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build a candidate Checkmarx One OpenAPI spec and drift report.")
    add_arguments(ap)
    return run(build_cfg(ap.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
