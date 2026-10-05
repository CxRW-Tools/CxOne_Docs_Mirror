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
| `--auth-token-env VAR` | API: opt in to authenticated fetching of the extra services (see below); `VAR` names an environment variable holding a tenant bearer token |
| `--allow-shrink` | API: write the spec even if the shrink guard fires (see below) |

API exit codes: `0` ok, `1` fatal (source unreachable or needs a login), `10` breaking drift on an endpoint in `--used-endpoints`, `11` the shrink guard fired and `cxone_openapi.json` was not written.

No credentials are used by default. The `ai-triage` and `remediation` `openapi.json` files need a login, so they are reported and skipped unless you opt in with `--auth-token-env`.

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
| `api-spec-drift.json` | api-spec | Diff against `--baseline`, keyed by `METHOD /path`: added, changed (with what changed), deprecated, removed candidates, baseline-only, stoplight-only, prefix-unknown, prefix-conflict, response-fields-dropped, overlay, and the shrink-guard result | Machine-readable drift; intersect with the endpoints your code calls | Rewritten each run |
| `api-spec-manifest.json` | api-spec | Services found, counts, the gateway-prefix map with confidence, unplaced operations, unresolved refs, repairs made, validation result | Audit how the spec was built | Rewritten each run |
| `history.json` | api-spec | Per-operation signatures from earlier runs | Detects removals (missing from live in two runs in a row) and keeps `x-live-verified` stable | Kept; updated only by runs that fetch |
| `raw/provenance.json` | api-spec | Source URL, fetch time, SHA-256 and status for every raw file | Traceability | Rewritten each fetch |
| `raw/live/` | api-spec | `swagger-starter.js`, `catalog.json` (service list), one `<SERVICE>.yaml` per live service, `extra-*.json` for extra services | The live source files, as downloaded | Cache; a failed fetch keeps the previous copy |
| `raw/stoplight/` | api-spec | `branches.json`, `toc.json`, `services.json`, one OpenAPI YAML per Stoplight API | The Stoplight source files, as downloaded | Same as above |
| `overlay/*.yaml` or `*.json` | you | Operations (same shape as the spec) that neither source has; applied after the merge and flagged when a source starts covering them, unless the operation sets `x-overlay-keep: true` | Keep hand-added endpoints across runs | Input; tracked in git |

Rebuild without the network using `--from-raw api/raw`. A run with `--dry-run` fetches but writes only the report.

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

`history.json` records per-operation signatures, so a change to placement logic can leave spurious `removed_candidate` entries behind. Its format carries a version; a file written by an older version is ignored automatically (the run notes it) and a fresh history starts. To reset by hand, delete `api/history.json`.

## Cleanup and safety

- Files are written to a `.tmp` sibling and renamed, so a killed run never leaves a half-written output. Any `.tmp` file left by an earlier crash is deleted at the start of the next run.
- The extract JSON is the only intermediate and is removed once combine succeeds.
- Everything else in `docs/` and `api/` is a deliverable, state or cache, as listed above.
- `docs/` and `api/` (except `api/overlay/`) are gitignored; only the tool is committed.

## License

MIT; see [LICENSE](LICENSE).

## Tests

```bash
python -m unittest discover -s tests
```
