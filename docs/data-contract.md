# Data Contract

One page: every field the engine writes, where it comes from, its type, how
it's derived, and what happens when an input is missing. The source files
this is assembled from stay the implementation — `dedupe_report.json`,
`modules/m1-enrichment/HUBSPOT_PUSH.md`, `enrich.py`'s `icp_tier()` /
`lifecycle_target()` / `region_for_country()`, and `config/icp.yaml` — read
those for the exact logic; this page is the summary.

## 1. Contact fields (`hubspot_ready.csv`, `hubspot_contacts.csv`)

`hubspot_ready.csv`'s full analyst-view column set is `FIELDS` in
[`enrich.py`](../modules/m1-enrichment/enrich.py) (line 2734); the subset
pushed to HubSpot's Contact object is `CONTACT_FIELDS` (no `industry`/
`numemployees` — those are Company-object properties), and of that, the
subset written as HubSpot *custom* properties is `CONTACT_PROPERTIES` in
[`push_to_hubspot.py`](../modules/m1-enrichment/push_to_hubspot.py) (line
81, via `build_contact_inputs()` at line 425). Everything else on the
contact (`email`, `firstname`, `lastname`, `jobtitle`, `company`, `country`)
is a HubSpot standard property, mapped 1:1.

| Field | Type / enum | Source | Derivation | If missing |
|---|---|---|---|---|
| `email` | string, HubSpot idProperty | registrant export | passthrough, lowercased for matching | row dropped — no HubSpot object without it |
| `firstname` / `lastname` | string | registrant export | passthrough | left blank; contact still created |
| `jobtitle` | string | registrant export | passthrough | `""` — flows into `function`/`seniority` below, which fall back to `"general"`/`"unknown"` |
| `company` | string | registrant export | passthrough, or `"Unknown"` sentinel | `"Unknown"` sets `industry="Unknown"` and (offline lane only) forces `numemployees=10` |
| `function` | enum: `executive`, `revops`, `customer_success`, `sales`, `marketing`, `general` | rule cascade, `classify_function()` (`enrich.py` line 452) | keyword match on `jobtitle`; live also runs the LLM row-inference prompt (`prompts/inference.md`) on rows the cascade can't resolve | falls through to `general` |
| `seniority` | enum: `c_suite`, `vp`, `head`, `director`, `manager`, `intern`, `individual_contributor`, `unknown` | `classify_seniority()` (`enrich.py` line 467) + live LLM inference, same as `function` | keyword/pattern match | falls through to `unknown` |
| `industry` (contact/company bucket) | string against `config/icp.yaml`'s tier-industry vocabulary (IT/ITES, BFSI, Manufacturing, Pharma, Retail, ... — see the file for Darwinbox's full list) or `Other`/`Unknown` | `industry_source`: `clay` \| `llm` \| `rules` | live: Clay's firmographics win when present, else the LLM firmographics prompt (`prompts/firmographics.md`, batched once per distinct company); the offline rule fallback, `classify_industry()` (`enrich.py` line 444), is a small fixed keyword table (fintech/saas/software/commerce/analytics/it services/cloud → `Fintech`/`SaaS`/`Ecommerce`/`IT Services`, else `Other`) that predates this event's Darwinbox vocabulary and won't produce most of `icp.yaml`'s labels | `company=="Unknown"` → `"Unknown"`; a non-ASCII-dominant name the rules table can't place sets `needs_review_reason=non_ascii_company_unclassified` and docks `confidence` by 0.30 (`enrich.py` line 2583) instead of counting as a confident match — live LLM resolves most of these and clears the flag (`enrich.py` line 1662) |
| `numemployees` | integer | `numemployees_source`: `clay` \| `llm` \| `synthetic` | live: Clay's employee-count wins when present, else the LLM firmographics estimate; `--offline` is the only lane where `synthetic_company_size(seed)` runs — a deterministic hash of the domain/name, **not a real headcount lookup** (`enrich.py` line 486) | `company=="Unknown"` → fixed `10`, `numemployees_source=synthetic` |
| `country` | string | registrant export | passthrough, normalised to ISO-2 (`normalise_country()`, `enrich.py` line 536 — Zoom/GoTo/ON24 all write country differently) | `""` → `region_for_country()` returns `"UNASSIGNED"` |
| `region` | enum: `INDIA`, `SEA`, `MENA`, `NA`, `UKEU` (Darwinbox's sales pods), or the legacy `AMER`/`EMEA`/`APAC` fallback (see §5), or `UNASSIGNED` | derived from `country` | `region_for_country()` (`enrich.py` line 546) — see §5 | unrecognized country → `UNASSIGNED` |
| `hubspot_owner_email` | string | derived from `region` | `config/icp.yaml`'s `icp.owners` map | see §5 — not every fallback path resolves to an owner today |
| `icp_tier` | enum: `tier1`, `tier2`, `tier3`, `unqualified` | rule cascade, `icp_tier()` (`enrich.py` line 564) — see §3 | title/size/industry checked against `config/icp.yaml`'s tiers; live also scores the same rubric with an LLM (`prompts/icp_scoring.md`) as a second opinion — a model tier more than one level off the rule tier still ships but sets `needs_review_reason=icp_disagreement` | title/size/industry outside every tier's bounds → `unqualified` |
| `icp_rationale` | free text | generated alongside `icp_tier` | one sentence naming which title/size/industry check passed or failed | always populated |
| `confidence` | float, starts at 1.0 | derived | decremented per uncertain step above (rule-table industry fallback, synthetic company size, weak dedupe match — see §2) — never a raw model logprob | n/a |
| `lifecyclestage` | enum, HubSpot standard property | derived | `lifecycle_target()` rubric (`enrich.py` line 594) — see §4; no-regression check against any pre-existing HubSpot stage (`LIFECYCLE_RANK`, `enrich.py` line 124, applied at line 1739) | new contact, no match → target stage applies directly |
| `hs_lead_status` | string | HubSpot read-back or `"NEW"` | `"NEW"` for a new contact; existing HubSpot value otherwise | n/a |
| `hubspot_contact_id` | string | HubSpot Search API (dedupe step, live) | populated on a HubSpot match | `""` for new contacts |
| `attendance_status` | enum: `attended`, `no_show` | registrant export | `r["attended"] == "Yes"` | absent input treated as not-attended |
| `time_in_session_minutes` | number | registrant export | passthrough — feeds the `>=25` check in §4 | missing/non-numeric → treated as not meeting the session-length bar |
| `registration_time` | timestamp | registrant export | passthrough | n/a |
| `merge_action` | enum: `create_new`, `update_existing:<hubspot_vid>` | dedupe-against-HubSpot step | see §2 | n/a |
| `needs_review` / `needs_review_reason` | boolean / enum (`non_ascii_company_unclassified`, `icp_disagreement`) | derived | set at the two points above; live LLM resolution clears the industry one, never the ICP-disagreement one (that always wants a human look) | default `false` / `""` |
| `company_domain` | string | derived | resolved from email domain, freemail domains excluded (`FREEMAIL_DOMAINS`, `enrich.py` line 120) | `""` → no Company object created for this contact (still gets a Contact row) |
| `event_tag` | string | orchestrator | one tag per run — the M1 push's `postevent_event` property, the filter every read-back (M4's `sync`, `--verify`) uses | n/a — always set |

## 2. Dedupe (`dedupe_report.json`)

Method, unchanged from `enrich.py` (`LOCAL_WEIGHT=0.25`, `IDENT_WEIGHT=0.75`,
`DEDUPE_THRESHOLD=0.80`, lines 99–101):

```
combined = 0.25 * ratio(email_localpart_a, email_localpart_b)
         + 0.75 * ratio(f"{first} {last} {company_key}"_a, f"{first} {last} {company_key}"_b)
```

`ratio()` is `difflib.SequenceMatcher.ratio()`, case-insensitive. A pair at
`combined >= 0.80` is the same person — within the batch
(`dedupe_within_batch()`, `enrich.py` line 653) and against live HubSpot
contacts via the Search API (`dedupe_against_hubspot()`, line 938, same
formula/threshold). A within-batch match is flagged `_merged_away` on the
losing row rather than deleted outright, so a case-only duplicate email
(`RHADDAD@…` vs `rhaddad@…`) merges into its primary instead of dropping
both rows. A HubSpot match sets `merge_action = update_existing:<vid>` and,
below 0.95, docks `confidence` proportionally (`enrich.py` line 2619)
rather than treating every match at the threshold as equally certain. Pairs
scoring in `[0.65, 0.80)` are too ambiguous for the rule engine and go to
the LLM gray-zone adjudicator instead (capped at 20 pairs/run,
`prompts/dedupe_adjudication.md`).

This event's fixture is 150 registrant rows (`data/incoming/
registrants.csv`, 151 lines including header) plus the two speakers folded
in from `speakers.json`/`segments.json`. Live counts (excluded/duplicate/
HubSpot-matched/output rows) are per-run, written fresh to
`dedupe_report.json` — don't trust a fixed number here over that file. A
checked-in 30-row live slice with real counts is at
`out/verify-w1/m1/dedupe_report.json` and `out/receipts/m1-live-slice-30/`
(0 fake rows excluded, 0 within-batch duplicates against the post-purge
sandbox, 32 output rows, 9 gray-zone pairs adjudicated; 96.2% contact /
93.0% company verified completeness, per M1's own README) — cite that as a
slice, not a full-150-row result.

## 3. ICP tier rubric

`icp_tier()` (`enrich.py` line 564) checks, in order: **tier1** — title in
`icp.tiers.tier1.titles` and company size in `tier1.company_size` and
industry in `tier1.industries`; **tier2** — same shape against `tier2`'s
lists; **tier3** — catch-all, any title/industry, size in
`tier3.company_size`; else **unqualified**. The algorithm hasn't changed;
the data has — `config/icp.yaml` now encodes Darwinbox's own buyer (HR/
people leaders: CHRO, VP HR, Head of People, HRIS Lead, HR Ops Manager,
Payroll Manager, TA Manager, ... at tier1 = 500+ employees, tier2 = 200–500)
rather than a generic SaaS-marketing persona. Read the file for the current
title/size/industry bounds rather than trusting a copy here — it's the one
place this gets edited per event. Every row gets a rationale string naming
which check passed/failed, never just the label; live also runs the LLM
second-opinion check described in §1.

## 4. Lifecycle-stage rubric

From `lifecycle_target()` (`enrich.py` line 594) — unchanged since the v1
fix that replaced a blanket assignment which put most attendees at MQL
regardless of tier or attendance:

| Condition | Target stage |
|---|---|
| tier1 **and** attended **and** `time_in_session_minutes >= 25` | `marketingqualifiedlead` |
| tier1 or tier2 (attended **or** no-show) | `lead` |
| everything else (incl. `unqualified` tier) | `subscriber` |

Target only — the no-regression check (`LIFECYCLE_RANK`, `enrich.py` line
124, applied at line 1739) never demotes a pre-existing HubSpot stage below
its current rank.

## 5. Region + owner routing

From `config/icp.yaml`'s `icp.regions`/`icp.owners`, resolved by
`region_for_country()` (`enrich.py` line 546):

| Region (Darwinbox sales pod) | Countries | Owner |
|---|---|---|
| INDIA | IN | `sdr.india@darwinbox.com` |
| SEA | SG, MY, ID, PH, TH, VN | `sdr.sea@darwinbox.com` |
| MENA | AE, SA, QA, KW, OM, BH, EG | `sdr.mena@darwinbox.com` |
| NA | US, CA | `sdr.na@darwinbox.com` |
| UKEU | UK, GB, DE, FR, NL, IE | `sdr.ukeu@darwinbox.com` |

(The sandbox portal has one HubSpot owner user, so every pod's `owner_map`
currently points at that one account for the demo — see `icp.yaml`'s own
comment. `hubspot_owner_email` above is the CSV/routing value, independent
of that sandbox shortcut.)

A country not in that explicit list falls through to a second, built-in
table (`REGION_BY_COUNTRY`, `enrich.py` line 528) that still uses the old
`AMER`/`EMEA`/`APAC` region names from the pre-Darwinbox build — and
`icp.yaml`'s `owners` map has no entry under those names any more, so a
registrant whose country only matches this fallback table (e.g. AU, JP, ZA,
most of continental Europe) gets a region label but an **empty**
`hubspot_owner_email`, not a defaulted one. The 30-row live slice's
`hubspot_owner_email` completeness (90.6%, vs 100% for `region` — see §2)
is this gap showing up in a real run, not row-level data loss. Anything
matching neither table lands on `UNASSIGNED` with the same empty-owner
behaviour.

## 6. HubSpot push mechanics (the two hard-won gotchas)

Full detail: [`HUBSPOT_PUSH.md`](../modules/m1-enrichment/HUBSPOT_PUSH.md).
The two that aren't obvious from the API docs:

- **Company upsert can't use `idProperty=domain` with `batch/upsert`** —
  HubSpot doesn't unique-index company `domain`, and that call 400s.
  `push_to_hubspot.py` instead searches by `domain` first, then
  `batch/update` for hits and `batch/create` for misses
  (`build_company_inputs()`, line 405).
- **`industry` is a fixed enum** on the Company object. M1's labels are
  mapped through `HUBSPOT_INDUSTRY_ENUM` (`push_to_hubspot.py` line 386)
  before the write; an unmapped label is dropped from that record's
  payload, not sent raw. The table currently covers the old SaaS/Fintech/
  IT-Services/Ecommerce buckets plus a handful of Darwinbox ones
  (healthcare, retail, manufacturing) — several of `icp.yaml`'s own
  industries (BFSI, Pharma, Hospitality, Telecom, ...) have no enum mapping
  yet and drop from the Company payload silently until one's added.

## 7. Company fields (`hubspot_companies.csv`)

`COMPANY_FIELDS` (`enrich.py` line 2783): `domain`, `name`, `industry`,
`numemployees` — one row per resolvable domain, deduped by majority vote
across ties (`build_company_rows()`, `enrich.py` line 2934). Pushed
properties: `name`, `domain` (idProperty), `numberofemployees`, `industry`
(enum-mapped per §6), `event_tag` (`COMPANY_PROPERTIES`,
`push_to_hubspot.py` line 125). A row with no resolvable domain is excluded
from this file entirely — it still exists as a Contact row with a blank
`company_domain`, just with no Company object to associate to.

## 8. UTM taxonomy

Canonical parameter set, defined once in `with_utm()`
([`shared/utm.py`](../shared/utm.py)) and imported by both M2
(`modules/m2-comms/comms.py`) and M3 (`modules/m3-repurpose/repurpose.py`).
M2 always passes `webinar`/`email` (`UTM_SOURCE`/`UTM_MEDIUM` constants in
`comms.py`); M3 varies them per asset/channel (`CHANNEL_UTM` for blog/
YouTube, `SOCIAL_PLATFORM_UTM` per social platform — see
`modules/m3-repurpose/README.md`).

| Param | Value | Notes |
|---|---|---|
| `utm_source` | `webinar` for every M2 email; per-asset for M3 (`blog`, `youtube`, `linkedin`, `x`) | fixed per module/channel, not globally fixed |
| `utm_medium` | `email` for M2; per-asset for M3 (`content`, `video`, `social`) | same |
| `utm_campaign` | the event slug (e.g. `darwinbox-ai-in-hr-2026-08-13`) | one campaign per event, same slug in both modules so M2 and M3 links roll up together |
| `utm_content` | segment/variant identifier, e.g. `attendee-a`/`attendee-b` for A/B subject lines, or an asset-specific slug for M3 | distinguishes which specific send/asset a click came from |

## 9. What's not covered here

Owner *IDs* (`hubspot_owner_id`) resolve from `hubspot_owner_email` at
HubSpot import time, not written by this engine. Anomaly-detection and
lead-scoring fields on the M4 dashboard are computed metrics, not
enrichment fields pushed to HubSpot — out of scope for this contract; see
`modules/m4-dashboard/README.md` for that shape.
