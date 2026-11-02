---
name: cxone-mirror-docs
description: Bring every output of the Checkmarx docs mirror up to date, end to end - the OpenAPI spec candidate in api/, the combined guide, index and agent views in docs/, and the checked per-page companion written by subagents. Works from a fresh clone (installs dependencies, downloads everything) and for monthly updates (refreshes what is stale, enriches only new or changed pages). Use when asked to set up, fetch, update, refresh, rebuild, enrich or summarise the docs mirror, the API spec or the companion.
---

# Docs mirror: update everything, end to end

This repository produces two sets of files, both gitignored, so a fresh clone has neither:

- `api/`: `cxone_openapi.json` (a merged OpenAPI candidate) and `API-SPEC-REPORT.md` (drift, read first).
- `docs/`: the faithful guide `checkmarx-one-docs-<date>.md` and its parts, `-index.json`, `-review.md`,
  `-reference.md`, `-lookup.md`, and the **separate** LLM companion `-companion.jsonl`.

The goal of a run is that all of them are current. Fetching and building (steps 3-4) is plain Python: network,
no AI usage, no credentials. The only real token cost is the subagents in step 6.

Never edit generated files by hand and never write the enrichment results yourself: subagents write them and
`enrich-check` decides what is kept. Run every command from the repository root.

## Scope

By default do everything. If the user limits the scope ("just the API spec", "docs only, no enrichment",
"only enrich"), skip the other steps and say which were skipped in the report.

## Steps

1. **Check the machine.** Find a working Python (3.9+): try `python --version`, and on Windows `py --version`.
   Use whichever works for every command below. Then check the libraries:
   `python -c "import bs4, lxml, markdownify, httpx, yaml, openapi_spec_validator"`.
   If that fails, tell the user the packages in `requirements.txt` will be installed into that Python, and on
   their yes run `python -m pip install -r requirements.txt`. Do not use another install method.

2. **Look at the state and agree the plan.** Check, without changing anything:
   - API spec: does `api/cxone_openapi.json` exist, and how old is it (file modified time)?
   - Docs: run `python cx_docs_mirror.py --stage enrich-prep`. A plan with no warning means the guide is current;
     `WARNING: the guide is N days old` means stale; "no combined guide" means nothing has been fetched yet.

   Then tell the user, in one message, what you will do and ask once for a yes:
   - Fetch the API spec if it is missing, more than 7 days old, or the docs are being fetched (network, a few
     minutes, no AI). Ask whether they have a current spec to compare against (`--baseline <path>`, for example
     the Multi-Tool's `spec/cxone_openapi.json`; it is only read) and a list of endpoints their code calls
     (`--used-endpoints <path>`). Both are optional; use them only if the user gives a path.
   - Fetch the docs on a fresh install (about 500 pages, several minutes), or refresh them if stale (about
     4 minutes). Add `--fresh` only if the user asks to start clean.
   - Enrichment follows in step 5, which asks separately if it is big.

   If the user declines a fetch, use what is on disk and say so in the report; with no guide at all, stop after
   the API spec.

3. **API spec (no AI).** Run in the background (it can outlast a foreground command; do not poll, you are told
   when it ends): `python cx_docs_mirror.py --stage api-spec` plus `--baseline` / `--used-endpoints` if given.
   Do this before the docs: the docs analysis checks the guide's API paths against this spec. Exit codes:
   - `0`: fine. Read `api/API-SPEC-REPORT.md` for the report.
   - `10`: breaking drift on an endpoint the user listed. The spec was written; continue, and put those
     endpoints at the top of the report.
   - `11`: the shrink guard fired (the new spec is much smaller than the baseline, usually a partial fetch) and
     `cxone_openapi.json` was **not** written. Say so and offer one retry. Never add `--allow-shrink` unless the user asks.
   - `1`: fatal (a source was unreachable). Show the message, offer one retry, then continue with the docs.

   Credentials: the default run uses none. `ai-triage` and `remediation` need a login and are reported as skipped;
   the overlay in `api/overlay/` already covers them. Only if the user asks to fetch those live: they set a tenant
   bearer token in an environment variable themselves, and you pass its **name** with `--auth-token-env VAR`.
   Never ask for the token in chat, never write it to a file, never print it.

4. **Docs (no AI).** Run in the background: `python cx_docs_mirror.py --stage enrich-prep --refresh`. It mirrors,
   extracts, combines, runs the analysis and views, then prints the enrichment plan. Exit codes:
   - `0`: continue.
   - `3`: a quality gate failed (printed above). Report the metric and stop. Do not pass `--no-quality-gate`
     unless the user asks.
   - `4`: too many pages could not be fetched. Offer one retry; pages already fetched are kept, so it is quicker.
   - Anything else: show the last lines of output and stop.

   If the docs were current but the API spec was fetched in step 3, run `python cx_docs_mirror.py --stage analyze`
   and then `--stage views` instead, so the lookup's API-path statuses use the new spec.

5. **Confirm cost on big runs.** If more than 100 pages need a record (the first run does: about 500 pages and
   roughly 2.8M tokens), tell the user the estimate and ask before continuing. A monthly run is normally a few
   dozen pages and needs no confirmation. If the user declines, skip to step 9; the guide, views and lookup are
   built and usable, with the first sentence of each page standing in for a summary.

6. **Run the batches.** For each `docs/enrich-work/batch-NNN.json`, start a subagent (model `sonnet`, at most 4
   at a time, one message per group) with this brief:

   > Read `docs/enrich-work/prompt.md` and `docs/enrich-work/batch-NNN.json` and follow the instructions.
   > Write the JSON array of records to `docs/enrich-work/result-NNN.json` (the file named in the batch).
   > Do not read or change any other file.

7. **Check.** Run `python cx_docs_mirror.py --stage enrich-check`. It validates every record against its
   source page (numbers, names, commands, paths and menu entries must appear on the page), stores the good
   ones, rebuilds the companion and lists the rejected ones in `docs/enrich-work/rejected.json`.
   If anything was rejected, run `--stage enrich-prep` again (only those pages come back, in new batches),
   repeat step 6 and this step once, then stop. Do not loosen the checks and do not hand-edit records.

8. **Rebuild the views.** Run `python cx_docs_mirror.py --stage views` so the reference view picks up the summaries.

9. **Report.** Keep it short:
   - What ran and what was skipped or declined (install, API spec, docs fetch or refresh, enrichment), with the
     date of the guide and spec now on disk.
   - API spec: operation count and the top of `API-SPEC-REPORT.md` (breaking drift first, fetch problems).
   - Docs: pages, quality line, and the analysis counts printed by `analyze`.
   - Enrichment: pages with a record, rejected after the retry (most common reasons), approximate tokens.
   - Which files to load: `docs/...-reference.md` first, then the guide; `api/cxone_openapi.json` for endpoints.

10. **Deliver.** Always the last step, after the report, on every run that produced output (including a run where
    the user declined enrichment). Send the deliverables to the user with `SendUserFile` (status `proactive`),
    one call, only the files that exist on disk for this run:
    - `api/cxone_openapi.json`
    - `api/API-SPEC-REPORT.md`
    - `docs/checkmarx-one-docs-<date>-reference.md`
    - `docs/checkmarx-one-docs-<date>.md`
    - `docs/checkmarx-one-docs-<date>-companion.jsonl`

    Use `<date>` from the guide's filename. Do not send the other `docs/` files or the `enrich-work/`, `enrich-cache/`
    or `pages/` folders; they are working files. If the run stopped before a file was built, leave it out and say so
    in the report.

## Notes

- Records are cached by page text, prompt version and (for variants) the canonical page's text, in
  `docs/enrich-cache/`. A run with no changed pages makes no LLM calls.
- Changing the prompt in `cx_docs_enrich.py` means bumping `PROMPT_VERSION`, which re-runs every page.
- `docs-synonyms.json` (optional, repo root, curated by a person) maps a term to aliases the pages never
  spell out, for example `{"AI Triage": ["Triage Assist"]}`. The checks accept those aliases.
- Without Claude Code, `python cx_docs_mirror.py` does steps 3-4 (API spec, then docs). The enrichment batches
  need subagents, so they only run through this skill.
- Do not state or infer GA / beta / early-access status anywhere in the records.
