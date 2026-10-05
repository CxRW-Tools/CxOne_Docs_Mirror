# CxOne Docs Mirror

Downloads Checkmarx documentation into two separate sets of outputs:

| Job | Source | Output |
|---|---|---|
| **User docs** | docs.checkmarx.com | one canonical Markdown file, plus a change report |
| **API spec** | live tenant catalog + Stoplight API reference | one merged OpenAPI 3.0.3 candidate, plus a drift report |

Nothing is written to your existing spec; the API stage produces a candidate to review.

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

Other stages: `mirror`, `extract`, `combine`, `compress` (each runs on its own).

### User docs

Crawls the docs nav for the products in `INCLUDE_PRODUCTS`, rewrites only pages whose content changed, and combines them into `checkmarx-one-docs-<date>.md`. Settings live in the CONFIG block at the top of `cx_docs_mirror.py`.

### API spec

Implemented in `cx_api_spec.py` (also runnable directly). Everything goes to `api-spec/`:

| File | Contents |
|---|---|
| `cxone_openapi.json` | merged spec (live schemas, Stoplight descriptions and examples) |
| `API-SPEC-REPORT.md` | short summary, highest risk first |
| `api-spec-drift.json` | added / changed / deprecated / removed per `METHOD /path` |
| `api-spec-manifest.json` | services, gateway-prefix map, unplaced operations, repairs |
| `raw/` | downloaded files with source URL, time and SHA-256 |
| `history.json` | per-operation signatures between runs |

Useful flags:

- `--baseline FILE` diff against your current spec.
- `--overlay DIR` hand-maintained operations that neither source has (default `api-overlay/` if present).
- `--used-endpoints FILE` exit with code 10 if drift touches an endpoint you depend on.
- `--dry-run` fetch and write only the report.
- `--from-raw DIR` rebuild offline from earlier downloads.
- `--probe` confirm gateway prefixes with a few unauthenticated GETs.
- `--skip-validation` skip the OpenAPI 3.0 validator, which otherwise runs on every merge.

No credentials are used. Services that need a login (`ai-triage`, `remediation` openapi.json) are reported and skipped.

## Tests

```bash
python -m unittest discover -s tests
```

## Output files

Generated data (`cx-docs/`, dated `.md` files, `manifest.json`, `api-spec/`, and so on) is gitignored; only the tool is committed.
