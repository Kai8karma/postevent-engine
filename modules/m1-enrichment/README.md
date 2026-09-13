# M1 -- Lead List Enrichment

Run: `python3 enrich.py --out out/selftest-m1` (live lane, default). Push:
`python3 push_to_hubspot.py --dry-run` -- see [`HUBSPOT_PUSH.md`](HUBSPOT_PUSH.md).

## Input

`data/incoming/registrants.csv` -- a 150-row Zoom Webinar Registrants export
for Darwinbox's real public HR webinar (see `data/incoming/README.md`):
registrant names/emails are synthetic, employer domains are real companies so
firmographic lookups return real data. Optional inputs: `--config`
(`config/icp.yaml` -- tiers, regions, owners, suppression list), `--hubspot`
(`data/fixtures/hubspot_existing.json`, the fixture dedupe candidate set),
`--speakers`/`--segments` (`data/incoming/speakers.json` +
`data/fixtures/segments.json`, folded in as `evangelist`-lifecycle contacts).

## AI role

Live is the default lane (see "How to run"). The LLM (`run_llm_enrichment()`,
`prompts/*.md`) does four jobs, each checked or capped by rule-engine code
rather than trusted blind:
- **Row inference** (`prompts/inference.md`) -- title/function/seniority for
  contacts the rule cascade couldn't resolve.
- **Firmographics** (`prompts/firmographics.md`) -- per-company industry
  (from `config/icp.yaml`'s tier-industry vocabulary) + employee-count
  estimate + confidence, batched once per distinct company.
- **ICP tier** (`prompts/icp_scoring.md`) -- tier + rationale + confidence,
  checked against `icp_tier()`'s deterministic formula as validator; a model
  tier more than one level from the rule tier still ships, but sets
  `needs_review_reason=icp_disagreement` for a human to adjudicate.
- **Gray-zone dedupe adjudication** (`prompts/dedupe_adjudication.md`) --
  pairs scoring in `[0.65, 0.80)`, too ambiguous for the fixed threshold,
  capped at 20 pairs/run.

Lifecycle stage is **not** inferred -- `lifecycle_target()` derives it from
tier + attendance + session length by a pinned rubric, only overwriting an
existing HubSpot stage when the target outranks it.

A parse failure or unreachable backend degrades that batch to the rule-table
result with a warning. `--offline` is the only lane where
`synthetic_company_size()` runs, always labelled `numemployees_source=synthetic`.

## Tools

`LLM_BACKEND` (env, `auto|claude|openrouter`, default `auto` -- tries
`claude -p` then OpenRouter), `OPENROUTER_MODEL` (comma-separated chain,
overrides the built-in fallback list), `OPENROUTER_MAX_TOKENS`,
`LLM_BATCH_DEADLINE_S` (per-batch wall-clock budget, default 420s -- free-tier
models are slow), `LLM_BATCH_ROWS` (default 25), `LLM_FIRMO_BATCH` (default
40 companies/call), `LLM_DEBUG` (verbose call logging). `OPENROUTER_API_KEY`
env, or a `KEY=VALUE` line in `~/.config/postevent/llm.env`.

## Output (in `--out`)

- `hubspot_ready.csv` -- analyst view: Contact + Company columns combined,
  plus provenance columns `icp_source`, `industry_source`,
  `numemployees_source` (each `clay|llm|rules|synthetic`), `icp_confidence`,
  `firmographics_confidence`, `needs_review_reason`.
- `hubspot_companies.csv` / `hubspot_contacts.csv` -- the two
  HubSpot-pushable CSVs (Company vs. Contact object properties, never
  mixed). Push/import companies first.
- `dedupe_report.json` -- fuzzy-match pairs, threshold, and (when any
  gray-zone pair fired) `gray_zone_adjudication`.
- `quality_report.json` -- per-field completeness: `spec_completeness_raw_pct`
  (anything non-blank) vs. `spec_completeness_verified_pct` (excludes the
  synthetic size and generic fallbacks; industry/size only count when the
  source is `clay`, or `llm` with confidence >= 0.7). Top-level `pass` is
  verified-vs-90%; `pass_raw` is the raw figure -- report `pass`.
- `enriched.json` -- JSON mirror of `hubspot_ready.csv`.
- `live_inference_report.json` -- batch counts, rows patched, parse-failure
  counts; written whenever the live lane runs.
- `<out>/receipts/m1_llm_calls.json` / `m1_hubspot_dedupe.json` -- see
  "Receipts" below.

## How to run

- **Live (default)**: `python3 enrich.py --out out/run1` -- rule cascade
  first, LLM fills what's left, dedupes against the live HubSpot Search API
  when `HUBSPOT_TOKEN` resolves (else `hubspot_dedupe_source=unavailable`).
- **Offline**: `python3 enrich.py --offline --out out/run1-offline` -- zero
  network/LLM/HubSpot calls, prints `[lane] offline`.
- **With Clay results**: `--emit-clay-domains out/run1/clay_domains.json` on
  a run writes the domains still missing/low-confidence firmographics; feed
  that list through the n8n Clay table
  (`orchestrator/n8n/railway/README.md`), then re-run with
  `--clay-results <callback-output>.json` -- Clay values overwrite the LLM's
  and set `*_source=clay`.
- **Push**: `python3 push_to_hubspot.py --in out/run1 --dry-run`, then drop
  `--dry-run` -- see `HUBSPOT_PUSH.md`.
- Other flags: `--limit-rows N` (iterate cheaply against the free-tier
  OpenRouter quota), `--live-dry-run` (print the exact live prompts, zero
  network), `--hubspot-fixture` (dedupe against the JSON fixture instead of
  the live portal), `--live` / `--hubspot-dedupe` (accepted no-ops -- live
  and live-dedupe are already the default).

## Receipts

Every LLM call appends to `<out>/receipts/m1_llm_calls.json`; every HubSpot
dedupe search appends to `<out>/receipts/m1_hubspot_dedupe.json`
(`hubspot_dedupe_source` records which: `live`, `live_error`, `unavailable`,
or `fixture`). A real 30-row live slice is checked in at
`out/receipts/m1-live-slice-30/` -- 96.2% contact / 93.0% company verified
completeness, 10 LLM calls, 7 parsed cleanly; cite it as a slice, not the
full-file result.

## Known limits

- The bundled OpenRouter key is free-tier (~50 requests/day) and can be slow
  enough to hit `LLM_BATCH_DEADLINE_S`; a timed-out batch degrades to the
  rule-table result, never a crash.
- The rule engine always has an answer -- the LLM is a second opinion on ICP
  tier, never the only source of one.
