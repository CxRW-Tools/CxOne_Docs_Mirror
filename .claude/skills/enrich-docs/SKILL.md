---
name: enrich-docs
description: Write per-page summaries, aliases and variant notes for the Checkmarx docs mirror into the separate companion file, using subagents in batches. Use when asked to enrich, summarise or refresh the docs companion, or after a monthly docs mirror run. Only pages that are new or changed are processed.
---

# Enrich the docs mirror (companion file)

The mirror (`docs/checkmarx-one-docs-<date>.md`) is faithful and deterministic. This skill adds a
**separate** companion, `docs/checkmarx-one-docs-<date>-companion.jsonl`, with a short checked record
per page. Never edit the guide or any other generated file by hand, and never write the result files
yourself: subagents write them, and `enrich-check` decides what is kept.

Run from the repository root. Python needs no credentials; the only cost is the subagents' tokens.

## Steps

1. **Prepare.** Run `python cx_docs_mirror.py --stage enrich-prep`. It prints how many pages need a record,
   the number of batches and a token estimate. If it says nothing to do, stop and say so.
2. **Confirm cost on big runs.** If more than 100 pages need a record (the first run does, about 500 pages
   and roughly 0.9M input tokens), tell the user the estimate and ask before continuing. A monthly run is
   normally a few dozen pages and needs no confirmation.
3. **Run the batches.** For each `docs/enrich-work/batch-NNN.json`, start a subagent (model `sonnet`, at most 4
   at a time, one message per group) with this brief:

   > Read `docs/enrich-work/prompt.md` and `docs/enrich-work/batch-NNN.json` and follow the instructions.
   > Write the JSON array of records to `docs/enrich-work/result-NNN.json` (the file named in the batch).
   > Do not read or change any other file.

4. **Check.** Run `python cx_docs_mirror.py --stage enrich-check`. It validates every record against its
   source page (numbers, names, commands, paths and menu entries must appear on the page), stores the good
   ones, rebuilds the companion and lists the rejected ones in `docs/enrich-work/rejected.json`.
5. **Retry once.** If anything was rejected, run `--stage enrich-prep` again (only the missing pages come back,
   in new batches), repeat steps 3-4 once, then stop. Do not loosen the checks and do not hand-edit records.
6. **Rebuild the views.** Run `python cx_docs_mirror.py --stage views` so the reference view picks up the summaries.
7. **Report.** Say how many pages now have a record, how many were rejected after the retry (and the most
   common reasons from `rejected.json`), and the approximate tokens used. Pages without a record are
   fine: the views fall back to the first sentence of the page.

## Notes

- Records are cached by page text, prompt version and (for variants) the canonical page's text, in
  `docs/enrich-cache/`. A run with no changed pages makes no LLM calls.
- Changing the prompt in `cx_docs_enrich.py` means bumping `PROMPT_VERSION`, which re-runs every page.
- `docs-synonyms.json` (optional, repo root, curated by a person) maps a term to aliases the pages never
  spell out, for example `{"AI Triage": ["Triage Assist"]}`. The checks accept those aliases.
- Do not state or infer GA / beta / early-access status anywhere in the records.
