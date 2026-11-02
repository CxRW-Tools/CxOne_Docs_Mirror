# CxOne Docs Mirror

Downloads Checkmarx documentation as two independent jobs. Each writes to its own folder, and nothing is written anywhere else.

| Job | Source | Output folder | Main deliverable |
|---|---|---|---|
| **User docs** | docs.checkmarx.com | `docs/` | one canonical Markdown file (plus separate API-reference and CxQL-reference parts), a page index, and compact views for agents |
| **API spec** | live tenant catalog + Stoplight API reference | `api/` | one merged OpenAPI 3.0.3 candidate spec |

The API stage never touches your existing spec; it produces a candidate and a drift report to review.

## Setup

Python 3.9+.

```bash
python -m pip install -r requirements.txt
```

## Usage

```bash
python cx_docs_mirror.py                      # API spec, then user docs (the docs analysis checks API paths against the new spec)
python cx_docs_mirror.py --stage docs         # mirror -> extract -> combine -> analyze -> views
python cx_docs_mirror.py --stage api-spec --baseline path/to/cxone_openapi.json
python cx_docs_mirror.py --help               # every flag
```

Other stages: `mirror`, `extract`, `combine`, `compress`, `analyze`, `views` (each runs on its own), and `enrich-prep` / `enrich-check` for the optional LLM step (see [Agent views and the companion](#agent-views-and-the-companion)). Settings for the docs job live in the CONFIG block at the top of `cx_docs_mirror.py`; the API job's defaults are at the top of `cx_api_spec.py`.

Useful flags:

| Flag | Effect |
|---|---|
| `--fresh` | start clean without deleting anything by hand: re-download every docs page and ignore the saved mirror state; for the API stage ignore `history.json` and the cached downloads (no stale fallbacks) and drop old raw files |
| `--keep-temp` | keep the temporary `docs/cx-extracted.json` after combine |
| `--images strip\|absolute` | docs: drop images (keeping meaningful alt text, the default) or rewrite them to docs.checkmarx.com URLs |
| `--api-reference separate\|inline` | docs: write API-reference pages to their own file (default) or keep them inline |
| `--cxql-reference separate\|inline` | docs: write the CxQL method reference (one ~330 KB page) to its own file (default) or keep it inline |
| `--stale-months N` | docs: front-matter index of pages not modified for N months (default 18, 0 = off) |
| `--max-failure-rate F` | docs: the mirror exits 4 (after writing its files) when more than this share of pages could not be checked (default 0.02; 1 disables) |
| `--no-quality-gate` | docs: do not fail combine or views (exit 3) when a quality metric exceeds its limit |
| `--baseline FILE` | API: diff against your current spec |
| `--overlay DIR` | API: hand-maintained operations neither source has (default `api/overlay/`) |
| `--used-endpoints FILE` | API: exit with code 10 if drift touches an endpoint you depend on |
| `--dry-run` | API: fetch and write only the report |
| `--from-raw DIR` | API: rebuild offline from earlier downloads |
| `--probe` | API: confirm gateway prefixes with a few unauthenticated GETs |
| `--skip-validation` | API: skip the OpenAPI 3.0 validator (it runs by default) |
| `--auth-token-env VAR` | API: opt in to authenticated fetching of the extra services (see below); `VAR` names an environment variable holding a tenant bearer token |
| `--allow-shrink` | API: write the spec even if the shrink guard fires (see below) |

API exit codes: `0` ok, `1` fatal (source unreachable or needs a login), `10` breaking drift on an endpoint in `--used-endpoints`, `11` the shrink guard fired and `cxone_openapi.json` was not written.

No credentials are used by default. The `ai-triage` and `remediation` `openapi.json` files need a login, so they are reported and skipped unless you opt in with `--auth-token-env`.

## Output files

```
docs/                                   user docs (generated, gitignored)
  checkmarx-one-docs-<date>.md
  checkmarx-one-docs-<date>-api-reference.md
  checkmarx-one-docs-<date>-cxql-reference.md
  checkmarx-one-docs-<date>-index.json
  checkmarx-one-docs-<date>-clusters.json
  checkmarx-one-docs-<date>-review.md
  checkmarx-one-docs-<date>-reference.md
  checkmarx-one-docs-<date>-lookup.md
  checkmarx-one-docs-<date>-companion.jsonl     only after /cxone-mirror-docs
  enrich-cache/, enrich-work/                   only after /cxone-mirror-docs
  changes-<date>.md
  mirror-state.json
  manifest.json
  failures.log                          only when a page failed
  pages/en/...                          cached HTML
  cx-extracted.json                     temporary, removed after combine

api/                                    API spec (generated, gitignored except overlay/)
  cxone_openapi.json
  API-SPEC-REPORT.md
  api-spec-drift.json
  api-spec-manifest.json
  history.json
  raw/...                               downloads with provenance
  overlay/                              your hand-maintained operations (input, tracked)
```

### `docs/` (user docs)

| File | Written by | Contents | Use | Lifecycle |
|---|---|---|---|---|
| `checkmarx-one-docs-<date>.md` | combine (compress rewrites it) | All kept pages in one Markdown file: doc > product > page, in site-navigation order, with a contents list and a stale-pages index. Every page heading carries a stable anchor (`{#p-<topic id>}`). `<date>` is the run date. | **The deliverable.** Feed it to a knowledge base or search. | Kept; one new file per run date |
| `checkmarx-one-docs-<date>-api-reference.md` | combine | The ~30 API-reference pages (they overlap the API spec job), same format. Absent with `--api-reference inline` | Keep the user guide and the API reference separate | Kept; one per run date |
| `checkmarx-one-docs-<date>-cxql-reference.md` | combine | The CxQL method reference ("SAST Query Language APIs", about 330 KB), same format. Absent with `--cxql-reference inline` | Keep a large, rarely needed reference out of the guide | Kept; one per run date |
| `checkmarx-one-docs-<date>-index.json` | combine | One entry per page in every file above: id (anchor), title, breadcrumb path, product, file, part, source page, modified date (ISO), size in bytes, flags (`stale`, `upcoming-section`, `section-index`) and aliases | A map an agent can load before reading the guide | Kept; one per run date |
| `checkmarx-one-docs-<date>-clusters.json` | analyze | Clusters of near-identical pages (for example one Developer Assist page per IDE) with a suggested canonical page, pages that sit inside a larger page, Upcoming Features pages that also exist as ordinary pages, and API paths the guide mentions with their status in the spec | Input to the views and the enrichment | Kept; one per run date |
| `checkmarx-one-docs-<date>-review.md` | analyze | The same findings as a short report for a person. Informational; it never fails a run | Occasional review | Kept; one per run date |
| `checkmarx-one-docs-<date>-reference.md` | views | Compact reference: one entry per page (title, id link, where it sits, summary, UI path / CLI / API when known). Variant pages are listed under their canonical page with what differs. About 3% of the guide's size | **Load this first** to find the right page, then open the page in the guide | Kept; one per run date |
| `checkmarx-one-docs-<date>-lookup.md` | views | Where to find things: CLI commands, API paths (with their status in the spec), UI locations and aliases, each pointing at pages | Look up a command, endpoint or term | Kept; one per run date |
| `checkmarx-one-docs-<date>-companion.jsonl` | enrich-check | One record per page written by an LLM and checked against the page: type, summary, aliases, UI path, CLI commands, API paths, and for variant pages what differs from the canonical page | Richer summaries in the reference view. **Never part of the guide** | Rebuilt from `enrich-cache/`; one per run date |
| `enrich-cache/`, `enrich-work/` | enrich-prep / enrich-check | Accepted records keyed by page text and prompt version; batches, instructions and results of the current run | Makes a rerun free when nothing changed | Cache; deleting it re-runs the LLM step for every page |
| `changes-<date>.md` | mirror | Pages new, updated, and removed from the nav since the previous run | Review what changed in the docs | Kept; one per run date |
| `mirror-state.json` | mirror | Per-URL hash, title, product, modified dates and position in the site navigation (`nav`) | Lets the next run skip unchanged pages | Kept; do not delete or every page is treated as new |
| `manifest.json` | mirror | The page files that are live in this run | Makes extract ignore stale cached pages | Rewritten each mirror run |
| `failures.log` | mirror | URLs that could not be fetched | Find pages to retry | Rewritten each run; removed when nothing failed |
| `pages/en/...` | mirror | Cached HTML of each mirrored page | Source for extract; allows change detection | Cache; safe to delete (the next mirror refetches everything) |
| `cx-extracted.json` | extract | Per-page records with Markdown bodies and filter status | Hand-off from extract to combine, and handy for auditing what was dropped | **Temporary.** Deleted after combine unless `--keep-temp` |

### `api/` (API spec)

| File | Written by | Contents | Use | Lifecycle |
|---|---|---|---|---|
| `cxone_openapi.json` | api-spec | The merged OpenAPI 3.0.3 spec. Schemas, parameters, enums and required-ness come from the live catalog; summaries, descriptions and examples from Stoplight. Every operation carries `x-source`, `x-service`, `x-gateway-prefix` and `x-live-verified`. Sorted and stable, so unchanged sources give an identical file. | **The deliverable.** A candidate to review and then adopt. | Rewritten each run |
| `API-SPEC-REPORT.md` | api-spec | Short summary of the drift, highest risk first, plus validation result and fetch problems | Read this first | Rewritten each run; the only file `--dry-run` writes |
| `api-spec-drift.json` | api-spec | Diff against `--baseline`, keyed by `METHOD /path`: added, changed (with what changed), deprecated, removed candidates, baseline-only, stoplight-only, prefix-unknown, prefix-conflict, response-fields-dropped, overlay, and the shrink-guard result | Machine-readable drift; intersect with the endpoints your code calls | Rewritten each run |
| `api-spec-manifest.json` | api-spec | Services found, counts, the gateway-prefix map with confidence, unplaced operations, unresolved refs, repairs made, validation result | Audit how the spec was built | Rewritten each run |
| `history.json` | api-spec | Per-operation signatures from earlier runs | Detects removals (missing from live in two runs in a row) and keeps `x-live-verified` stable | Kept; updated only by runs that fetch |
| `raw/provenance.json` | api-spec | Source URL, fetch time, SHA-256 and status for every raw file | Traceability | Rewritten each fetch |
| `raw/live/` | api-spec | `swagger-starter.js`, `catalog.json` (service list), one `<SERVICE>.yaml` per live service, `extra-*.json` for extra services | The live source files, as downloaded | Cache; a failed fetch keeps the previous copy |
| `raw/stoplight/` | api-spec | `branches.json`, `toc.json`, `services.json`, one OpenAPI YAML per Stoplight API | The Stoplight source files, as downloaded | Same as above |
| `overlay/*.yaml` or `*.json` | you | Operations (same shape as the spec) that neither source has; applied after the merge and flagged when a source starts covering them, unless the operation sets `x-overlay-keep: true` | Keep hand-added endpoints across runs | Input; tracked in git |

Rebuild without the network using `--from-raw api/raw`. A run with `--dry-run` fetches but writes only the report.

## How the docs document is built

Every transformation below is deterministic: the same cached HTML always gives the same Markdown. The renderer is `cx_docs_render.py`; the stage logic is in `cx_docs_mirror.py`.

| Concern | What happens |
|---|---|
| Duplicate pages | One page per topic id (`34965-<id>-...`). The original slug wins over a `...-<id>` re-publication under another parent, then the earlier navigation position. Identical visible text is a second pass. Dropped pages are remembered as aliases, so links to them still land on the survivor. |
| Headings | A page is an H3; its own headings nest below it. Anything that would pass H6 becomes a bold lead-in instead of being squashed into H6. |
| Anchors | Each page heading is `### Title {#p-<topic id>}`, keyed on the topic id so links survive title changes. Pages that share a title get the breadcrumb tail (`General Settings (User Management)`). |
| Links | Links between pages in the same file become `#p-<id>`. UUID fragments are dropped. Links to pages that are not in the file (excluded products, or the other part) point at docs.checkmarx.com. In-page jumps keep just the words. |
| Images | Stripped by default (a meaningful alt text is kept as `[Image: ...]`; file-name alt text is dropped). `--images absolute` keeps them as docs.checkmarx.com URLs. A relative image path is never emitted. |
| Callouts | Note / Notice / Tip / Warning blocks and plain sidebars become `> **Note:** ...` blockquotes. Expandable (accordion) sections are content, not callouts: they become a bold title followed by ordinary text, and the "Expand source" control text is dropped. |
| Tables | Simple tables stay pipe tables. Tables with block content in a cell (lists, code, several paragraphs) and every nested table become definition-list style bullets. |
| Order | Pages follow the site navigation order recorded by the mirror stage, not the alphabet. Run the mirror stage once to record it. |
| Parts | Pages under a breadcrumb matching `API_REFERENCE_CRUMBS` go to `...-api-reference.md`; the page matching `CXQL_REFERENCE_PATTERNS` goes to `...-cxql-reference.md`. The guide says how many pages moved. |
| Page header | Under each page title: `Path:` (breadcrumb), `Modified:`, `Source:` and, when set, `Flags:`, one per line. Flags are `stale` (older than `--stale-months`), `upcoming-section` (under Upcoming Features) and `section-index` (see next row). |
| Section pages | A page with no text of its own but with child pages lists those children (in navigation order). A page with only a title and no children is dropped. |
| Staleness | The front matter lists pages last modified more than `--stale-months` months ago, oldest first. |

The mirror is polite about rate limits. On HTTP 429 every worker pauses together, using the server's `Retry-After` when it sends one and otherwise backing off 15s, 30s, 60s (up to 120s), and the per-page delay grows a little for the rest of the run. Pages that still fail get one more try after a short cool-down. If more than 2% of pages still cannot be checked, the mirror exits with code 4 after writing its files, so extract and combine never run on silently stale copies. A page that fails keeps its cached copy, and `failures.log` lists it.

At the end of combine a quality line is printed: dead links, broken images, H6 headings, duplicate ids, toggle leftovers. `QUALITY_LIMITS` in `cx_docs_mirror.py` sets the limits; dead links, broken images, duplicate ids and toggle leftovers must be 0 or combine exits with code 3 (after writing the files, and keeping `cx-extracted.json` for inspection). H6 headings are reported only. Use `--no-quality-gate` to ignore the limits.

## Agent views and the companion

The guide stays a faithful, deterministic mirror. Everything below is built from it and written to separate files, so nothing generated is ever mistaken for documentation. Anything the guide does not state (such as GA or early-access status) is not added.

**Deterministic (no LLM, part of `--stage docs`).**

- `analyze` finds clusters of near-identical pages (8-word shingles, Jaccard 0.5; a cluster is a seed page plus the pages at least that similar to it), pages at least 80% contained in another page, Upcoming Features pages that also exist as ordinary pages, and `/api/...` paths the guide mentions that the spec does not have. Thresholds are constants at the top of `cx_docs_analysis.py`.
- `views` writes the reference view and the lookup tables. Every link must point at a page in the guide, or `views` exits with code 3 (`--no-quality-gate` ignores that). Without a companion, summaries are the first sentence of the page.
- Rebuilding from the same cached pages gives byte-identical files.

**LLM step (optional, monthly, run in Claude Code).** `enrich-prep` finds pages whose record is missing or out of date and writes batches; the `/cxone-mirror-docs` skill (`.claude/skills/cxone-mirror-docs/`) has subagents write a record per page; `enrich-check` validates and stores them and rebuilds `...-companion.jsonl`; then run `views` again.

```bash
python cx_docs_mirror.py --stage enrich-prep    # cost estimate and batches (add --refresh to rebuild the docs first)
# in Claude Code: /cxone-mirror-docs
python cx_docs_mirror.py --stage enrich-check   # validate, cache, rebuild the companion
python cx_docs_mirror.py --stage views
```

A record is stored only if every number, quoted span, flag, URL, path, command, menu entry and name-like token (CamelCase, ALL CAPS, snake_case) in it also appears on the source page, aliases appear on the page (or in the optional, hand-written `docs-synonyms.json`, `{"term": ["alias"]}`), and `variant_of` comes from the clusters, not the LLM. Failures are listed in `docs/enrich-work/rejected.json` and retried once; a page without a record just falls back to its first sentence.

`enrich-prep` warns when the guide it reads is older than `--max-guide-age-days` (default 7; 0 turns it off), because the records would describe old pages. `--refresh` runs mirror, extract, combine, analyze and views first, then prepares the batches; it needs the network but uses no AI. The skill asks before refreshing.

**Everything from Claude Code.** After a fresh clone you can skip the terminal: open the repo in Claude Code and run `/cxone-mirror-docs`. It brings every output up to date: it checks for Python and the libraries in `requirements.txt` (and asks before installing them), fetches the API spec (optionally against your `--baseline` and `--used-endpoints`), downloads or refreshes the docs, and then does the enrichment. It states its plan and asks once before fetching (network, no AI), and again before a large enrichment. It never handles credentials: for `--auth-token-env` you set the variable yourself and the skill passes only its name. You can limit the scope, for example "just the API spec" or "docs only, no enrichment".

Records are cached by page text, prompt version and, for a variant, the canonical page's text. A month with no changed pages makes no LLM calls and rewrites the companion identically. Bump `PROMPT_VERSION` in `cx_docs_enrich.py` when you change the prompt (this re-runs every page). Cost: a 16-page pilot used about 3.5 billed tokens per token of page text, which is what `enrich-prep` uses for its estimate; expect roughly 2.8M tokens for the first full run and a few hundred thousand for a monthly one. No credentials are involved.

## How the API spec is built

### Where each service is placed

The live YAML's own `servers[0].url` is the authority for a service's gateway prefix. The baseline spec is **never** used as evidence, so the same raw files give a byte-identical `cxone_openapi.json` with or without `--baseline`; the baseline only affects the drift report.

| `x-prefix-confidence` | Meaning |
|---|---|
| `matched` | The live prefix and a Stoplight service agree (several non-trivial operations match) |
| `declared` | Only the live `servers[0].url` says so |
| `probed` | `declared`, and `--probe` saw the route exist (401/403/405) |
| `conflict` | Stoplight matches several operations at a *different* prefix. Operations are placed at the live prefix and listed under "Prefix problems" in the report and `prefix_conflict` in the drift file. A human decides |
| `overlay` | Comes from the overlay |
| unknown | No usable prefix (for example `INTEGRATIONS_REPOS`, whose `servers[0].url` is `REPOS`); operations are left unplaced and listed in the manifest |

A Stoplight match only counts when at least two operations match, and a bare `/` or `/{id}` never counts (nearly every service has one).

### Drift severity

Each changed operation is `breaking` or `additive`. `--used-endpoints` exits with code 10 only for **breaking** drift on an endpoint you listed (a removed or missing operation, or a breaking change). Additive drift on your endpoints is listed in the report but does not fail the run.

Breaking: a removed parameter (except an optional header), a parameter or request field that became required, an enum value removed from an input, a type change, a removed field in a 2xx response or in a request, a 2xx response field that is no longer guaranteed, a changed authentication requirement, or a missing operation. Everything else, including new optional parameters, new fields, new enum values, constraint tweaks and changes to error bodies, is additive.

### Overlay and the AI services

`api/overlay/from-baseline.json` holds operations that no source provides. `api/overlay/ai-services-authenticated.json` holds the live `ai-triage` and `remediation` operations that the unauthenticated sources lack: `POST /api/ai-triage/v2/triage`, `POST /api/remediation/v2/remediate`, `GET /api/remediation/remediation/{remediation_id}/export`, and the full live `POST /api/remediation/remediate` (Stoplight has that one but omits `projectID`, so it carries `x-overlay-keep: true` and is never reported as retirable).

To regenerate the AI file, fetch both specs with a tenant token, then inline every `$ref` so the file is self-contained:

```bash
curl -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/ai-triage/openapi.json"
curl -H "Authorization: Bearer $TOKEN" "$BASE_URL/api/remediation/openapi.json"
```

Or let the tool fetch them: set a token in an environment variable and pass its name.

```bash
export CX_TOKEN=...           # tenant bearer token; never put it in a file
python cx_docs_mirror.py --stage api-spec --auth-token-env CX_TOKEN
```

The token is read from the environment, sent only to the `--api-base-url` host for those two requests (never to Stoplight, never followed across redirects), and is not written to disk or printed. Use a token for the same tenant as `--api-base-url`. The downloaded specs land in `api/raw/live/extra-*.json` and are merged as live sources, after which the overlay entries become redundant (the report says so). **No credentials may be committed to this repository.**

### Shrink guard

A partial fetch could silently produce a smaller spec. With `--baseline`, the run exits with code `11` and does **not** write `cxone_openapi.json` (or update `history.json`) when either holds:

- the merged spec has more than 5% fewer operations than the baseline;
- any `--used-endpoints` entry is missing from the merged spec.

`API-SPEC-REPORT.md` is always written, starts with a "SHRINK GUARD FIRED" section naming the check, and lists fetch problems at the bottom. Pass `--allow-shrink` to write the spec anyway (the report then says the guard was overridden). Without `--baseline` the guard does not run.

### Response fields dropped (informational)

Live schemas can omit fields that really exist: on a real tenant `GET /api/projects/{id}` returns `repoId`, `privatePackage` and `imported_proj_name`, but the live schema lists none of them. The `response_fields_dropped` drift category lists, per `METHOD /path`, the 2xx response properties that the baseline or Stoplight has and the merged operation lacks (nested ones as `tags.test`, array items as `items[].id`). It never fails a run and never alters the spec; it exists so a reviewer checks those operations before adopting. Against an unchanged baseline the baseline-derived part is empty; Stoplight-derived entries remain for as long as Stoplight and live disagree.

### What this tool cannot see

It runs unauthenticated, so it cannot see what needs a login or what no published source describes: the `ai-triage` and `remediation` specs, credits, and routes you only know from an authenticated tenant. Cover those with your own overlay (`--overlay`); an overlay entry is the right place for them, and the report tells you when a source starts covering one.

If you use `--auth-token-env`, it needs an already-issued bearer token, and `--api-base-url` must point at the tenant that issued it (the default is the US host, `https://ast.checkmarx.net`). The DEU and US live catalogs were identical for all 70 services on 2026-10-05, so the default region is fine for the catalog itself.

### Known limits

- **Cross-file `$ref`s.** Stoplight files sometimes `$ref` a sibling file that is not in the table of contents (for example `sastResults_copy.yaml`). The tool fetches those from the same Stoplight project into `api/raw/stoplight/refs/` and resolves against them. A name the project does not have stays unresolved and is listed in the manifest and report, never guessed. Today that is `Scans.yaml` (one Reports parameter) and `multiEngineResults.yaml` (three Best Fix Location schemas, from the live catalog, which has no sibling files), plus five unresolved local refs in the baseline-derived overlay.
- **`SAST_QUERIES_AUDIT`** is placed at `/api/cx-audit` and flagged `conflict`. `/queries` is served there, but `GET /sessions` is 404 under `/api/cx-audit` and 405 under `/api/query-editor` (405 means the route exists for another method). Treat the `sessions` paths as unconfirmed, and do not "fix" this without evidence from a tenant.
- **`INTEGRATIONS_REPOS`** declares the unusable prefix `REPOS` in the live catalog, so its operations stay unplaced.
- **Line endings.** Output is LF. Git on Windows may check files out as CRLF, so normalise line endings before comparing a file with a fresh run.

### Adopting the output

The adoption procedure lives in the consumer repo: see "Refreshing the spec" in [`spec/CLEANUP_NOTES.md`](https://github.com/CxRW-Tools/CxOne_Multi-Tool/blob/main/spec/CLEANUP_NOTES.md).

### Resetting `history.json`

The easy way is `--fresh` (see Usage): it ignores the saved history and cached downloads for that run and rebuilds them, so nothing has to be deleted by hand. Files that a sync client (OneDrive) refuses to delete are reported and ignored.

`history.json` records per-operation signatures, so a change to placement logic can leave spurious `removed_candidate` entries behind. Its format carries a version; a file written by an older version is ignored automatically (the run notes it) and a fresh history starts. To reset by hand, delete `api/history.json`.

## Cleanup and safety

- Files are written to a `.tmp` sibling and renamed, so a killed run never leaves a half-written output. Any `.tmp` file left by an earlier crash is deleted at the start of the next run.
- The extract JSON is the only intermediate and is removed once combine succeeds.
- Everything else in `docs/` and `api/` is a deliverable, state or cache, as listed above. Files from earlier run dates are never deleted for you.
- `docs/` and `api/` (except `api/overlay/`) are gitignored; only the tool is committed.

## License

MIT; see [LICENSE](LICENSE).

## Tests

```bash
python -m unittest discover -s tests
```
