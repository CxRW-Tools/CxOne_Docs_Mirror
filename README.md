# CxOne Docs Mirror

Downloads Checkmarx documentation as two independent jobs. Each writes to its own folder, and nothing is written anywhere else.

| Job | Source | Output folder | Main deliverable |
|---|---|---|---|
| **User docs** | docs.checkmarx.com | `docs/` | one canonical Markdown file |
| **API spec** | live tenant catalog + Stoplight API reference | `api/` | one merged OpenAPI 3.0.3 candidate spec |

The API stage never touches your existing spec; it produces a candidate and a drift report to review.

## Setup

Python 3.9+.

```bash
python -m pip install -r requirements.txt
```

## Usage

```bash
python cx_docs_mirror.py                      # user docs + API spec
python cx_docs_mirror.py --stage docs         # mirror -> extract -> combine
python cx_docs_mirror.py --stage api-spec --baseline path/to/cxone_openapi.json
python cx_docs_mirror.py --help               # every flag
```

Other stages: `mirror`, `extract`, `combine`, `compress` (each runs on its own). Settings for the docs job live in the CONFIG block at the top of `cx_docs_mirror.py`; the API job's defaults are at the top of `cx_api_spec.py`.

Useful flags:

| Flag | Effect |
|---|---|
| `--keep-temp` | keep the temporary `docs/cx-extracted.json` after combine |
| `--baseline FILE` | API: diff against your current spec |
| `--overlay DIR` | API: hand-maintained operations neither source has (default `api/overlay/`) |
| `--used-endpoints FILE` | API: exit with code 10 if drift touches an endpoint you depend on |
| `--dry-run` | API: fetch and write only the report |
| `--from-raw DIR` | API: rebuild offline from earlier downloads |
| `--probe` | API: confirm gateway prefixes with a few unauthenticated GETs |
| `--skip-validation` | API: skip the OpenAPI 3.0 validator (it runs by default) |

No credentials are used. API services that need a login (`ai-triage` and `remediation` `openapi.json`) are reported and skipped.

## Output files

```
docs/                                   user docs (generated, gitignored)
  checkmarx-one-docs-<date>.md
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
| `checkmarx-one-docs-<date>.md` | combine (compress rewrites it) | All kept pages in one Markdown file: doc > product > page, with a contents list. `<date>` is the run date. | **The deliverable.** Feed it to a knowledge base or search. | Kept; one new file per run date |
| `changes-<date>.md` | mirror | Pages new, updated, and removed from the nav since the previous run | Review what changed in the docs | Kept; one per run date |
| `mirror-state.json` | mirror | Per-URL hash, title, product and modified dates | Lets the next run skip unchanged pages | Kept; do not delete or every page is treated as new |
| `manifest.json` | mirror | The page files that are live in this run | Makes extract ignore stale cached pages | Rewritten each mirror run |
| `failures.log` | mirror | URLs that could not be fetched | Find pages to retry | Rewritten each run; removed when nothing failed |
| `pages/en/...` | mirror | Cached HTML of each mirrored page | Source for extract; allows change detection | Cache; safe to delete (the next mirror refetches everything) |
| `cx-extracted.json` | extract | Per-page records with Markdown bodies and filter status | Hand-off from extract to combine, and handy for auditing what was dropped | **Temporary.** Deleted after combine unless `--keep-temp` |

### `api/` (API spec)

| File | Written by | Contents | Use | Lifecycle |
|---|---|---|---|---|
| `cxone_openapi.json` | api-spec | The merged OpenAPI 3.0.3 spec. Schemas, parameters, enums and required-ness come from the live catalog; summaries, descriptions and examples from Stoplight. Every operation carries `x-source`, `x-service`, `x-gateway-prefix` and `x-live-verified`. Sorted and stable, so unchanged sources give an identical file. | **The deliverable.** A candidate to review and then adopt. | Rewritten each run |
| `API-SPEC-REPORT.md` | api-spec | Short summary of the drift, highest risk first, plus validation result and fetch problems | Read this first | Rewritten each run; the only file `--dry-run` writes |
| `api-spec-drift.json` | api-spec | Diff against `--baseline`, keyed by `METHOD /path`: added, changed (with what changed), deprecated, removed candidates, baseline-only, stoplight-only, prefix-unknown, overlay | Machine-readable drift; intersect with the endpoints your code calls | Rewritten each run |
| `api-spec-manifest.json` | api-spec | Services found, counts, the gateway-prefix map with confidence, unplaced operations, unresolved refs, repairs made, validation result | Audit how the spec was built | Rewritten each run |
| `history.json` | api-spec | Per-operation signatures from earlier runs | Detects removals (missing from live in two runs in a row) and keeps `x-live-verified` stable | Kept; updated only by runs that fetch |
| `raw/provenance.json` | api-spec | Source URL, fetch time, SHA-256 and status for every raw file | Traceability | Rewritten each fetch |
| `raw/live/` | api-spec | `swagger-starter.js`, `catalog.json` (service list), one `<SERVICE>.yaml` per live service, `extra-*.json` for extra services | The live source files, as downloaded | Cache; a failed fetch keeps the previous copy |
| `raw/stoplight/` | api-spec | `branches.json`, `toc.json`, `services.json`, one OpenAPI YAML per Stoplight API | The Stoplight source files, as downloaded | Same as above |
| `overlay/*.yaml` or `*.json` | you | Operations (same shape as the spec) that neither source has; applied after the merge and flagged when a source starts covering them | Keep hand-added endpoints across runs | Input; tracked in git |

Rebuild without the network using `--from-raw api/raw`. A run with `--dry-run` fetches but writes only the report.

## Cleanup and safety

- Files are written to a `.tmp` sibling and renamed, so a killed run never leaves a half-written output. Any `.tmp` file left by an earlier crash is deleted at the start of the next run.
- The extract JSON is the only intermediate and is removed once combine succeeds.
- Everything else in `docs/` and `api/` is a deliverable, state or cache, as listed above.
- `docs/` and `api/` (except `api/overlay/`) are gitignored; only the tool is committed.

## Tests

```bash
python -m unittest discover -s tests
```
