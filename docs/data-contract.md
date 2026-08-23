# Data Contract

One page: every field the engine writes, where it comes from, its type, how
it's derived, and what happens when an input is missing. This is the
canonical reference — the four source files it's assembled from
(`dedupe_report.json`, `modules/m1-enrichment/HUBSPOT_PUSH.md`,
`enrich.py::lifecycle_target()`, `config/icp.yaml`) stay the implementation,
this page is the summary a reviewer or a new SDR reads first. Every claim
below cites the code it came from — read that file if you need the exact
logic, not this page's paraphrase of it.

## 1. Contact fields (`hubspot_ready.csv`, `hubspot_contacts.csv`)

`hubspot_ready.csv`'s full 22-column analyst view is `FIELDS` in
[`enrich.py`](../modules/m1-enrichment/enrich.py) (line ~1295); the subset
actually pushed to HubSpot's Contact object is `CONTACT_FIELDS` (no
`industry`/`numemployees` — those are Company-object properties) and, of
that, the subset HubSpot's private app writes as *custom* properties is
`CONTACT_PROPERTIES` in
[`push_to_hubspot.py`](../modules/m1-enrichment/push_to_hubspot.py) (line
68). Everything else on the contact (`email`, `firstname`, `lastname`,
`jobtitle`, `company`, `country`) is a HubSpot **standard** property, mapped
1:1, no transform.

| Field | Type / enum | Source | Derivation | If missing |
|---|---|---|---|---|
| `email` | string, HubSpot idProperty | registrant export | passthrough, lowercased for matching | row dropped — no HubSpot object without it (`build_contact_inputs` skips it, counted in `skipped`) |
| `firstname` / `lastname` | string | registrant export | passthrough | left blank; contact still created (email is the only hard requirement) |
| `jobtitle` | string | registrant export | passthrough | `""` — flows into `function`/`seniority` classifiers below, which fall back to `"general"`/`"unknown"` |
| `company` | string | registrant export | passthrough, or `"Unknown"` sentinel | `"Unknown"` sets `industry="Unknown"` and forces `synthetic_company_size` to `10` (`enrich.py` ~1197) instead of a hashed size |
| `function` | enum: `executive`, `revops`, `customer_success`, `sales`, `marketing`, `general` | derived from `jobtitle` | keyword match in `classify_function()` (`enrich.py` line 232) — e.g. `"marketing"` in title → `marketing` | falls through to `general` |
| `seniority` | enum: `c_suite`, `vp`, `head`, `director`, `manager`, `intern`, `individual_contributor`, `unknown` | derived from `jobtitle` | keyword/pattern match in `classify_seniority()` (`enrich.py` line 247) | falls through to `unknown` |
| `industry` (contact-side bucket) | string label (e.g. `SaaS`, `Fintech`) or `Other`/`Unknown` | derived from `company` name | keyword match in `classify_industry()` (`enrich.py` line 224); offline-only — `--live` runs the same classification through the LLM instead and can resolve names the ASCII keyword table can't | `company=="Unknown"` → `industry="Unknown"`; a non-ASCII-dominant name that falls through to `Other` sets `needs_review=true` and docks `confidence` by 0.30 (judge fix #7, `enrich.py` line 1205) rather than silently counting as a confident match |
| `numemployees` | integer | synthetic | `synthetic_company_size(seed)` — a deterministic hash of the company's domain (or normalized name for freemail domains), **not a real headcount lookup** (`enrich.py` line 266); `confidence` is docked 0.05 on every row for this reason | `company=="Unknown"` → fixed `10` |
| `country` | string | registrant export | passthrough | `""` → `region_for_country` returns `"UNASSIGNED"` |
| `region` | enum: `AMER`, `EMEA`, `APAC`, `UNASSIGNED` | derived from `country` | `region_for_country()` (`enrich.py` line 326): first checks `config/icp.yaml`'s explicit `icp.regions` map, then falls back to the larger built-in `REGION_BY_COUNTRY` table (`enrich.py` line 329, ~30 more ISO-2 codes), then `UNASSIGNED` | unrecognized country → `UNASSIGNED`, owner still deterministically assigned (next row) |
| `hubspot_owner_email` | string (CSV-only — see note) | derived from `region` | `config/icp.yaml`'s `icp.owners` map, one SDR email per region | unrecognized region falls back to the **AMER** owner as a deterministic default (`enrich.py` line 336) rather than leaving ownership blank |
| `icp_tier` | enum: `tier1`, `tier2`, `tier3`, `unqualified` | derived from `jobtitle` + `numemployees` + `industry` | `icp_tier()` rubric (`enrich.py` line 344) — see §3 | title/size/industry outside every tier's bounds → `unqualified` |
| `icp_rationale` | free text | generated alongside `icp_tier` | one sentence naming which title/size/industry check passed or failed (`enrich.py` line 354) | always populated — the rubric always returns a rationale string |
| `confidence` | float, starts at 1.0 | derived | decremented per uncertain step above (non-ASCII industry fallback, synthetic company size, weak dedupe match) — never a raw model logprob | n/a |
| `lifecyclestage` | enum, HubSpot standard property | derived | `lifecycle_target()` rubric (`enrich.py` line 374) — see §4; **no-regression check**: if the row already matched an existing HubSpot contact, the target only applies if it outranks that contact's current stage (`LIFECYCLE_RANK`, `enrich.py` ~1206–1220) | new contact, no match → target stage applies directly |
| `hs_lead_status` | string | HubSpot read-back or `"NEW"` | `"NEW"` for `merge_action=create_new`; existing HubSpot value otherwise | n/a |
| `hubspot_contact_id` | string | HubSpot read-back | populated only when `merge_action` starts with `update_existing` | `""` for new contacts |
| `attendance_status` | enum: `attended`, `no_show` | registrant export | `r["attended"] == "Yes"` | absent input treated as not-attended |
| `time_in_session_minutes` | number | registrant export | passthrough — feeds the `>=25` check in the lifecycle rubric | missing/non-numeric → treated as not meeting the session-length bar |
| `registration_time` | timestamp | registrant export | passthrough | n/a |
| `merge_action` | enum: `create_new`, `update_existing:<hubspot_vid>` | dedupe-against-HubSpot step | see §2 | n/a — always one or the other |
| `needs_review` | boolean | derived | `true` when the industry classifier fell through to `Other` on a non-ASCII-dominant company name (offline path only — `--live`'s LLM classification resets it to `false` when it resolves the name, `enrich.py` line 810/1035) | default `false` |
| `company_domain` | string | derived | resolved from email domain (freemail domains excluded — see `FREEMAIL_DOMAINS`, `enrich.py` line 66) | `""` → no Company object created for this contact (still gets a Contact row) |
| `event_tag` | string | orchestrator | `slugify(event_name)-date`, same tag on every Contact/Company from one run — the filter every read-back (`--verify`, the dashboard's HubSpot cross-check) uses | n/a — always set by the orchestrator, not per-row |

## 2. Dedupe (`dedupe_report.json`)

Method, verbatim from `enrich.py` (`LOCAL_WEIGHT=0.25`, `IDENT_WEIGHT=0.75`,
`DEDUPE_THRESHOLD=0.80`):

```
combined = 0.25 * ratio(email_localpart_a, email_localpart_b)
         + 0.75 * ratio(f"{first} {last} {company_key}"_a, f"{first} {last} {company_key}"_b)
```

`ratio()` is `difflib.SequenceMatcher.ratio()`, case-insensitive. A pair at
`combined >= 0.80` is treated as the same person — within the batch
(`dedupe_within_batch`) and against existing HubSpot contacts
(`dedupe_against_hubspot`, same formula, same threshold). A within-batch
match keeps one row (`merge_action` unaffected); a HubSpot match sets
`merge_action = update_existing:<vid>` and, if the match score was below
0.95, docks `confidence` proportionally (`enrich.py` line 1226) rather than
treating every match at the threshold as equally certain. On this build's
fixture: 150 raw rows → 3 excluded as synthetic-fake → 12 within-batch
duplicate pairs → 16 HubSpot matches → 133 output rows, 117 net-new.

## 3. ICP tier rubric

From `config/icp.yaml` (this build's placeholder company, swap for a real
one — see README "Run it on your own webinar") and applied by
`icp_tier()` (`enrich.py` line 344), checked in order:

1. **tier1**: title in `icp.tiers.tier1.titles` (VP Marketing / Head of
   Demand Gen / Director Marketing Ops / CMO) **and** company size in
   `[200, 5000]` **and** industry in `[SaaS, Fintech, IT Services]`.
2. **tier2**: title in `tier2.titles` (Marketing Manager / Growth Manager /
   RevOps Manager) **and** size in `[50, 200]` **and** industry in
   `[SaaS, Fintech, IT Services, Ecommerce]`.
3. **tier3**: catch-all — any title, size in `[1, 50]`, any industry
   (wildcard).
4. **unqualified**: doesn't clear any of the above (most commonly: right
   title but company too big/small for that tier's band).

Every row gets a rationale string naming which check passed/failed — never
just the label.

## 4. Lifecycle-stage rubric

From `lifecycle_target()` (`enrich.py` line 374) — replaces an earlier
blanket `lifecycle_default` that put 91.4% of attendees at MQL regardless of
tier or attendance (judge fix #2; the old key is kept in `icp.yaml` for
backward-compat documentation only, not applied):

| Condition | Target stage |
|---|---|
| tier1 **and** attended **and** `time_in_session_minutes >= 25` | `marketingqualifiedlead` |
| tier1 or tier2 (attended **or** no-show) | `lead` |
| everything else (incl. `unqualified` tier) | `subscriber` |

This is the *target* only — `run_pipeline()` never demotes a pre-existing
HubSpot stage below its current rank (`LIFECYCLE_RANK`); a contact already
past the target keeps their current stage, and that's logged in a per-row
note (`enrich.py` line ~1211).

## 5. Region + owner routing

From `config/icp.yaml`'s `icp.regions`/`icp.owners`, resolved by
`region_for_country()` (`enrich.py` line 326):

| Region | Countries (icp.yaml explicit set) | Owner |
|---|---|---|
| AMER | US, CA, BR, MX | `sdr.amer@acmerevenue.example` |
| EMEA | UK, DE, FR, AE, ZA | `sdr.emea@acmerevenue.example` |
| APAC | IN, SG, AU, JP | `sdr.apac@acmerevenue.example` |

A country not in that explicit set falls through to the larger built-in
`REGION_BY_COUNTRY` table (~30 more ISO-2 codes across the same three
regions) before landing on `UNASSIGNED` — which still gets a deterministic
owner (the AMER SDR) rather than shipping a null owner field.

## 6. HubSpot push mechanics (the two hard-won gotchas)

Full detail: [`HUBSPOT_PUSH.md`](../modules/m1-enrichment/HUBSPOT_PUSH.md).
The two that aren't obvious from the API docs:

- **Company upsert can't use `idProperty=domain` with `batch/upsert`** —
  HubSpot does not unique-index company `domain`, and that call 400s.
  `push_to_hubspot.py` instead does search-by-`domain` first, then
  `batch/update` for hits and `batch/create` for misses.
- **`industry` is a fixed enum** on the Company object (e.g.
  `COMPUTER_SOFTWARE`, `FINANCIAL_SERVICES`,
  `INFORMATION_TECHNOLOGY_AND_SERVICES`) — M1's human-readable industry
  labels (`SaaS`, `Fintech`, ...) are mapped through `HUBSPOT_INDUSTRY_ENUM`
  in `push_to_hubspot.py` (line 270) before the write. An unmapped label is
  **dropped from that record's payload**, not sent raw (which would 400 the
  whole batch).

## 7. Company fields (`hubspot_companies.csv`)

`COMPANY_FIELDS` (`enrich.py` line 1337): `domain`, `name`, `industry`,
`numemployees` — one row per resolvable domain, deduped by majority vote
across ties on `(name, industry, numemployees)` (`build_company_rows()`,
`enrich.py` line ~1379). Pushed properties: `name`, `domain`
(idProperty), `numberofemployees`, `industry` (enum-mapped per §6),
`event_tag` (`COMPANY_PROPERTIES` / `build_company_inputs()`,
`push_to_hubspot.py` lines 108, 289). A row with no resolvable domain is
excluded from this file entirely — it still exists as a Contact row with a
blank `company_domain`, just with no Company object to associate to.

## 8. UTM taxonomy

Canonical parameter set, defined once in `with_utm()`
([`modules/m2-comms/comms.py`](../modules/m2-comms/comms.py) line 348) and
applied to every M2 email link today. **As of this write-up, M3's asset
links (recording URL, resource links in blog/social/YouTube copy) do not
yet call `with_utm()`** — `modules/m3-repurpose/repurpose.py` has no
`utm_` references. The parameter set below is the one M3 should adopt when
that lands, so a click on a repurposed asset attributes back through the
same UTM shape M2's emails use rather than a parallel scheme; check
`repurpose.py` directly before relying on M3 emitting these today:

| Param | Value | Notes |
|---|---|---|
| `utm_source` | `webinar` | fixed |
| `utm_medium` | `email` | fixed — set even on non-email assets, since the link originates from the webinar's comms/content package, not paid/organic |
| `utm_campaign` | `campaign_slug(event)` — `{slugified event name}-{date}`, e.g. `pipeline-after-the-webinar-2026-07-20` | one campaign per event |
| `utm_content` | segment/variant identifier, e.g. `speaker-a`/`speaker-b` for A/B subject lines, or an asset-specific slug for M3 content | distinguishes which specific send/asset a click came from |

## 9. What's not covered here

Owner *IDs* (`hubspot_owner_id`) are resolved from `hubspot_owner_email` at
HubSpot import time, not written by this engine (the sandbox has no owners
matching the CSV's SDR emails — see `HUBSPOT_PUSH.md`'s "Not pushed" note).
Anomaly-detection and lead-scoring fields on the M4 dashboard are computed
metrics, not enrichment fields pushed to HubSpot — they're out of scope for
this contract; see the dashboard's own output for that shape.
