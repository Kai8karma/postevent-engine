# Production Path: Clay Table Spec

**No Clay run has happened in this build.** Every firmographic value in this
package came from the LLM or the rule cascade. The receipt is
[`out/receipts/m1-live-slice-30/quality_report.json`](../../out/receipts/m1-live-slice-30/quality_report.json)
-- its `clay` block reads `clay_domains_supplied: 0`, `clay_domains_applied: 0`,
empty verified-domain lists and `run_urls: {}`. This page is therefore the
production *design*, not a record of something that ran.

`enrich.py` runs the rule+LLM equivalent of this table so the pipeline works
without a Clay account. Clay only ever reaches M1 through `--clay-results` --
the old Clay-CLI flag and its shell-out are gone; there is no other path in.
`tools/clay_enrich.py` is a standalone operator utility that would call the
`clay` CLI directly; nothing invokes it, it has never been run, and its output
is a run receipt rather than the `--clay-results` map, so it is not a second
door into M1 either. The trigger, wait, and evidence receipt around the
table live in n8n, not here -- see
[`orchestrator/n8n/railway/README.md`](../../orchestrator/n8n/railway/README.md)
for the full workflow (trigger curl, env vars, why the wait is time-based
rather than a Wait-node webhook resume).

## Column-by-column waterfall (matches `enrich.py::FIELDS`)

| # | Clay column | Enrichment | Fallback order | Demo equivalent |
|---|---|---|---|---|
| 1 | `Email Normalized` | Formula column: lowercase, trim | -- | `email` |
| 2 | `Name Normalized` | Formula column: title-case first/last | -- | `firstname`/`lastname` |
| 3 | `Is Fake/Test Row` | Formula column: regex against placeholder localparts/names | -- | fake-row flag (excluded upstream of `hubspot_ready.csv`, logged in `dedupe_report.json`) |
| 4 | `HubSpot Match` | Clay's native HubSpot enrichment (search by email, then fuzzy name+company match) | 1. exact email 2. fuzzy name+company | `merge_action` / `hubspot_contact_id` |
| 5 | `Company (resolved)` | Clearbit/Clay company enrichment by email domain | domain enrichment -> `prompts/inference.md` LLM column -> manual review | `company` (rule cascade: domain-peer lookup -> sibling-record backfill -> `"Unknown"`; live lane: `prompts/inference.md` batches rows still `"Unknown"`) |
| 6 | `Job Title (resolved)` | LinkedIn enrichment via Clay's People API keyed on email | LinkedIn lookup -> LLM inference -> generic fallback | `jobtitle` (rules: sibling backfill -> company-mode lookup -> `"Attendee"`; live: same `prompts/inference.md` batch) |
| 7 | `Industry` | Clearbit/Clay firmographic `industry` on the resolved company | company enrichment -> `prompts/firmographics.md` LLM classification | `industry` (rules: keyword table on company name; live: `prompts/firmographics.md` against `config/icp.yaml`'s vocabulary) |
| 8 | `Company Size` | Clearbit/Clay firmographic `employee_count` | company enrichment -> `prompts/firmographics.md` LLM estimate -> `--offline`-only synthetic bucket | `numemployees` (`--offline`: `synthetic_company_size()`'s deterministic hash bucket, never counted as verified) |
| 9 | `Function` / `Seniority` | Clay's title-parsing enrichment, or `prompts/inference.md`'s LLM columns when title itself came from LLM inference | -- | `function` / `seniority` |
| 10 | `ICP Tier` | Formula column mirroring `icp_tier()` (authoritative) + `prompts/icp_scoring.md` LLM second opinion, logged when confidence < 0.7 | rule engine authoritative; LLM disagreement > 1 tier level sets `needs_review_reason=icp_disagreement`, never a silent override | `icp_tier` / `icp_rationale` |
| 11 | `Region` / `Owner` | Formula column: country -> region, then `config/icp.yaml`'s owner-assignment table | -- | `region` / `hubspot_owner_email` (import-time lookup only -- HubSpot persists `hubspot_owner_id`) |
| 12 | `Lifecycle Stage` | Formula column applying the pinned rubric, then reading the existing HubSpot stage and only overwriting if the rubric's target outranks it | rubric target vs. existing stage, higher rank wins (never demotes) | `lifecyclestage` (`lifecycle_target()`, policy-derived, not inferred) |
| 13 | `Confidence` | Formula column: weighted average of per-column enrichment confidence | -- | `firmographics_confidence` / `icp_confidence` |
| 14 | `Needs Review` | Formula column: true when a lookup structurally couldn't resolve the row | -- | `needs_review_reason` (counted separately in `quality_report.json`, never folded into completeness) |
| 15 | `Company Domain` | Formula column: registrant email domain, excluded for freemail addresses | -- | `company_domain` -- the association key for the Company object |

## Clay table setup and the callback contract

The table itself (source column, enrichment columns, HTTP-API column) is
built once by the operator outside n8n; the exact steps and screenshots are
in `orchestrator/n8n/railway/README.md`'s "Clay table setup" section. The
parts that matter for M1's contract:

- **Source column** reads one row per webhook POST from n8n's
  `Push Domain to Clay` node: `{domain, run_id, callback_url}` -- one domain
  per row, not a table-level batch.
- **HTTP API column** fires after the enrichment columns resolve and POSTs
  back to that row's own `callback_url`:
  ```json
  {"domain": "...", "industry": "...", "employee_count": 1234,
   "country": "IN", "run_url": "https://app.clay.com/...", "run_id": "..."}
  ```
  n8n's `Clay Result Callback` webhook accumulates these (keyed by `run_id`)
  until `Assemble Clay Results` reads them back and hands the module API a
  `{run_id, clay_results}` body at the `finalize` phase.

## How Clay results reach M1

Only through `--clay-results PATH` -- a JSON file `{domain: {industry,
employee_count, country, source, run_url}}` (`enrich.py::load_clay_results()`;
a malformed file is a hard error, not a silent skip). `apply_clay_results()`
overwrites the LLM's/rule engine's values for each supplied domain and
relabels `industry_source`/`numemployees_source` to `clay` -- the strongest
rank in `SOURCE_RANK`, so a `clay` value is never displaced by a later LLM
pass. Domains still missing or below the firmographics confidence floor after
inference are the ones a run should send to Clay next; write them out with
`--emit-clay-domains PATH` (matches `docs/module-api.md`'s
`next.clay_domains`). The live slice did emit that list --
[`out/receipts/m1-live-slice-30/clay_domains.json`](../../out/receipts/m1-live-slice-30/clay_domains.json)
-- but no Clay run consumed it, so no `--clay-results` file was ever produced
and no field in this build carries `industry_source`/`numemployees_source` =
`clay`.

## What doesn't change between demo and production

The column *order*, *names*, and *fallback logic* above are the same whether
the waterfall runs in the Clay table against live APIs or in `enrich.py`
against the rule cascade + `prompts/inference.md` / `prompts/firmographics.md`
/ `prompts/icp_scoring.md`. Swapping from demo to production replaces each
enrichment step's *data source* (Clearbit/LinkedIn/Clay firmographics instead
of the LLM's estimate), not the pipeline's shape -- and `--clay-results` is
the only door that data walks through.
