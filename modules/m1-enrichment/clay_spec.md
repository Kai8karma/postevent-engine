# Production Path: Clay Table Spec

`enrich.py` is the offline stand-in for this table so the pipeline demos
without a Clay account. The column table below is generated straight from
`enrich.py`'s actual `FIELDS` list (not hand-maintained prose) so it can
never drift from the code the way it did before (judge fix #5) -- if a
column here doesn't match `hubspot_ready.csv`'s header, one of the two is
wrong.

## Source

Import table: webinar platform export (Zoom Webinar Registrants report),
same columns as `data/incoming/registrants.csv` (First Name, Last Name,
Email, Job Title, Company, Country/Region, Registration Time, Attended,
Time in Session). Trigger: manual CSV import for now; production trigger is
the webinar platform's post-event webhook -> n8n -> Clay import API (see
`orchestrator/n8n/m1-enrichment.json`).

## Column-by-column waterfall (matches `enrich.py::FIELDS` exactly)

| # | Clay column | Enrichment | Fallback order | Demo equivalent |
|---|---|---|---|---|
| 1 | `Email Normalized` | Formula column: lowercase, trim | -- | `email` |
| 2 | `Name Normalized` | Formula column: title-case first/last | -- | `firstname`/`lastname` |
| 3 | `Is Fake/Test Row` | Formula column: regex against placeholder localparts/names | -- | fake-row flag (excluded upstream of `hubspot_ready.csv`, logged in `dedupe_report.json`) |
| 4 | `HubSpot Match` | Clay's native HubSpot enrichment (search by email, then fuzzy name+company match) | 1. exact email 2. fuzzy name+company | `merge_action` / `hubspot_contact_id` |
| 5 | `Company (resolved)` | Waterfall: (a) Clearbit/Clay company enrichment by email domain -> (b) People Data Labs by company name -> (c) `prompts/inference.md` LLM column -> (d) manual review queue | domain enrichment -> name-search enrichment -> LLM inference -> human review | `company` (offline rule tables: domain-peer lookup -> sibling-record backfill -> `"Unknown"`; `--live`: `prompts/inference.md` batches rows still `"Unknown"` after the rule cascade) |
| 6 | `Job Title (resolved)` | Waterfall: (a) LinkedIn enrichment via Clay's People API keyed on email -> (b) if no LinkedIn match, `prompts/inference.md` LLM column classifying from company + peer titles | LinkedIn lookup -> LLM inference -> generic fallback | `jobtitle` (offline: sibling backfill -> company-mode lookup -> `"Attendee"` fallback; `--live`: same `prompts/inference.md` batch as company) |
| 7 | `Industry` | Clearbit/Clay firmographic `industry` field on the resolved company, cross-checked against `prompts/inference.md`'s LLM read of the company name for non-English/non-Latin names the keyword table can't parse | company enrichment -> LLM classification from company name/description | `industry` (offline: keyword table on company name; non-ASCII-dominant names that fall through to `"Other"` are flagged `needs_review` instead of counted as a confident match -- see quality_report's `needs_review_count`) |
| 8 | `Company Size` | Clearbit/Clay firmographic `employee_count` on the resolved company | company enrichment -> LLM estimate from company description -> `null` | `numemployees` (offline: **synthetic hash bucket, explicitly not real data** -- see `enrich.py::synthetic_company_size`) |
| 9 | `Function` / `Seniority` | Clay's built-in title-parsing enrichment (`Job Title Formatter`), or `prompts/inference.md`'s LLM columns when title itself came from LLM inference | -- | `function` / `seniority` |
| 10 | `ICP Tier` | Deterministic formula column mirroring `icp_tier()` (authoritative, unchanged by any LLM step) + `prompts/icp_scoring.md` LLM column producing a human-readable second opinion, logged for rows the rule engine scored at confidence < 0.7 | rule engine is authoritative; LLM second opinion + human review on disagreement, never a silent override | `icp_tier` / `icp_rationale` (offline+`--live` both write the same `icp_tier` value; `--live` appends the LLM's agree/disagree note to `icp_rationale`) |
| 11 | `Region` / `Owner` | Formula column: country -> region lookup, then a static owner-assignment table (or round-robin via Clay's built-in assignment logic) | -- | `region` / `hubspot_owner_email` (**import-time lookup only** -- HubSpot persists `hubspot_owner_id`, resolved from this email at import time, not this column itself) |
| 12 | `Lifecycle Stage` | Formula column applying the pinned rubric (tier1 + attended + session>=25min -> `marketingqualifiedlead`; tier1/tier2 attended or no-show -> `lead`; everyone else -> `subscriber`), then reading the existing HubSpot stage (from column 4's match) and only overwriting if the rubric's target outranks it | rubric target vs. existing stage, higher rank wins (never demotes) | `lifecyclestage` (offline: `LIFECYCLE_RANK` no-regression rule against `enrich.py::lifecycle_target()`) |
| 13 | `Confidence` | Formula column: weighted average of per-column enrichment confidence (Clay surfaces a confidence score per waterfall step) | -- | `confidence` |
| 14 | `Needs Review` | Formula column: true when a firmographic/industry lookup structurally couldn't resolve the row (e.g. non-Latin company name past the keyword classifier) | -- | `needs_review` (counted separately in `quality_report.json`, not folded into the completeness percentage) |
| 15 | `Company Domain` | Formula column: registrant email domain, excluded for freemail addresses | -- | `company_domain` -- the domain used to associate the Contact to its Company object on import (see below) |

## HTTP-to-HubSpot upsert -- two object types, not one (judge fix #3)

`hubspot_ready.csv` mixes Contact-object properties (e.g. `jobtitle`,
`function`) with Company-object properties (`industry`, `numemployees`) in
one row -- that's fine for a human reviewer, but HubSpot's API (and Clay's
"Send to HubSpot" action) needs them as two separate object writes. Clay's
final step is therefore **two** native "Send to HubSpot" actions, run in
this order:

1. **Companies first** -- one row per resolvable `company_domain`
   (`hubspot_companies.csv` in the demo lane): `domain`, `name`, `industry`,
   `numemployees`. Match on `domain`.
2. **Contacts second** -- `hubspot_contacts.csv` in the demo lane: Contact
   properties only (`jobtitle`, `company`, `function`, `seniority`,
   `hs_lead_status`, `lifecyclestage`, `hubspot_owner_email`, plus the three
   custom contact properties HubSpot doesn't have natively: `icp_tier`,
   `icp_rationale`, `confidence`), associated to the Company
   object created in step 1 via the `company_domain` key. **Match on**:
   `email` (primary), fallback to `merge_action` column's
   `hubspot_contact_id` when Clay's own match differs from the fuzzy-dedupe
   result (human review queue for disagreements, same principle as
   `icp_scoring.md`'s "no blind auto-override"). `hubspot_contact_id` itself
   is a demo join key for reconciling this run against `dedupe_report.json`
   -- it is never sent to HubSpot as a Contact property.
3. **Trigger**: manual "Push to HubSpot" button in Clay for the judgment
   gate (spec's "no blind auto-send" principle) -- reviewer scans the
   `confidence`, `needs_review`, and `icp_rationale` columns for anything
   under 0.7 / flagged before pushing.
4. **Rate limits**: HubSpot's private-app token allows 100 requests/10s;
   Clay batches the upsert accordingly, no manual throttling needed.

Rows with no resolvable `company_domain` (freemail registrants) still import
as Contacts in step 2, just without a Company association -- there's nothing
to associate them to.

## What doesn't change between demo and production

The column *order*, *names*, and *fallback logic* above are the same
whether the waterfall runs in Clay against live APIs or in `enrich.py`
against fixtures (offline rule tables by default, `prompts/inference.md` +
`prompts/icp_scoring.md` via `claude -p` under `--live`). Swapping from demo
to production is replacing each enrichment step's *data source*, not
redesigning the pipeline.
