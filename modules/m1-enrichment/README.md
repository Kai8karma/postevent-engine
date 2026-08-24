# M1 -- Lead List Enrichment

Run: `python3 enrich.py --out out/selftest-m1` (add `--in`, `--config`, `--hubspot` to point at other fixtures).
hubspot-push: `python3 push_to_hubspot.py --dry-run` (real CRM v3/v4 client for the two CSVs below + M2's `sends_log.json` -- see [`HUBSPOT_PUSH.md`](HUBSPOT_PUSH.md)).

Two lanes, both real:
- **Offline (default)** -- stdlib-only deterministic rule tables + lookup
  tables, zero network calls, reproducible. This is what the demo control
  room and CI run.
- **`--live`** -- the rule tables run first and stay authoritative for
  dedupe and the ICP tier formula; an LLM is then called as a second
  opinion: `prompts/inference.md` batches ~25 rows/call for contacts still
  missing title/company (or classified industry `"Other"`) after the rule
  cascade and backfills them from the model instead of the generic
  fallback, and `prompts/icp_scoring.md` batches rows with confidence < 0.7
  for a human-readable second opinion on the tier call (logged into
  `icp_rationale`, never auto-overriding the rule engine). Every batch call
  is wrapped so a parse failure or an unavailable backend degrades to the
  rule-table result with a warning -- `--live` never silently no-ops.
  - **Gray-zone dedupe adjudication**: dedupe pairs scoring in `[0.65, 0.80)`
    -- ambiguous enough that a fixed threshold can't call it, but not weak
    enough to dismiss -- are batched (one call, capped at `GRAY_ZONE_MAX_PAIRS`
    = 20 pairs) through `prompts/dedupe_adjudication.md`. The rule engine
    stays fully authoritative outside that band (`>=0.80` always merges,
    `<0.65` never does, `--live` or not); inside it, a `merge`/`no_merge`
    decision with a written rationale is applied the same way a
    >=threshold match would be, and surfaced into the row's
    `icp_rationale` (not only into `dedupe_report.json`). The prompt states
    the asymmetry up front (a false merge destroys a real lead; a false
    split only double-touches one person) so the model weighs the two
    failure modes differently, not just off the raw score. A parse failure
    or unavailable backend degrades every pair in the batch to `no_merge`
    -- the same default the rule engine already applies below threshold.
  - `LLM_BACKEND` (env, default `auto`): `auto` tries `claude -p` first and
    falls back to OpenRouter if a key is present; `claude` or `openrouter`
    force that backend only. Resolved once per `--live` run (a single
    preflight probe), not once per batch.
  - `OPENROUTER_API_KEY` (env, else parsed from `~/.config/postevent/llm.env`
    as a `KEY=VALUE` line): required for the OpenRouter backend; never
    logged.
  - `OPENROUTER_MODEL` (env, optional): overrides the default model
    (`anthropic/claude-sonnet-4.5`, with older Sonnet ids as further
    fallbacks in the source).
- **`--live-dry-run`** -- builds and prints the exact `--live` prompts +
  batch plan (also saved to `live_inference_report.json`) without calling
  `claude -p` at all. Zero network calls. Use this to verify the live path
  is wired correctly when `claude -p` auth is unavailable.

Proves: a messy 150-row Zoom registrant export -> fuzzy-deduped (within-batch + against a 40-contact HubSpot fixture, `difflib.SequenceMatcher`, threshold 0.80, documented in `dedupe_report.json`) -> every contact/company field inferred where missing (peer/company backfill cascade, then `--live`'s LLM second opinion for whatever's still unresolved) -> ICP-tiered against `config/icp.yaml` with a rationale string -> region/owner routed -> lifecycle-staged by a pinned tier/attendance/session-length rubric (never regressing an existing HubSpot contact), with rows the classifier structurally couldn't resolve flagged `needs_review` and counted separately rather than laundered into the completeness number.

**Completeness -- two numbers, not one.** `quality_report.json`'s `spec_completeness` reports both a **raw** and a **verified** figure per the dual-metric fix below; report whichever one you quote, don't blend them:
- **Raw** (`spec_completeness_raw_pct`, `completeness_pct`, `fields`): counts anything non-blank as filled, including two things that read as "data" but aren't -- `numemployees` from `synthetic_company_size()`'s deterministic hash placeholder (never a real firmographic lookup offline), and the rule cascade's generic fallbacks (`jobtitle` = `"Attendee"`, `industry` = `"Other"`) when nothing better resolved them. On the offline fixture this reads **contact 99.9% / company 97.3%** -- both clear >90%, but company's number is inflated by `numemployees` reading ~100% filled by construction.
- **Verified** (`spec_completeness_verified_pct`, `fields_verified`): the same fields with those two placeholders excluded from the numerator -- `numemployees` only counts when it's a real Clay `Enrich Company` result (`--live --clay-max`, see `run_clay_enrichment()`'s `numemployees_verified_domains`), and `jobtitle`/`industry` only count when they're not the generic fallback. On the offline fixture (zero `--clay-max` calls, so `numemployees` verifies at 0%) this reads **contact 87.8% / company 67.0%** -- both under the 90% bar. This is the honest number, reported as-is, not tuned to clear 90: the offline lane's synthetic company-size placeholder and the ~21% of rows the industry keyword classifier couldn't resolve (`synthetic_or_fallback_fields.industry_generic_fallback_count`) are real gaps, not just presentation. The path to verified >90% is the live Clay lane against real domains (see "Clay lane" below) -- `fixtures/clay_real_domains_registrants.csv`'s 8 real domains are the proof point that the mechanism itself works, not the synthetic default fixture.
- `needs_review_broadened_count`/`_pct` (quality_report.json, alongside the original narrower `needs_review_count`/`_pct` -- see its `needs_review_note`) is the row-level view of the same gap: any row still on a generic `jobtitle`/`industry` fallback that no `--live` LLM patch or `--clay-max` Clay call resolved.

## Outputs (in `--out`)

- `hubspot_ready.csv` -- **analyst view**, not for direct HubSpot import: one
  row per contact with both Contact- and Company-object properties combined
  for human review/QA. Import order for the two files below matters;
  importing this combined file directly would mix two HubSpot object types
  in one write.
- `hubspot_companies.csv` -- one row per resolvable `company_domain`
  (Company-object properties: `domain`, `name`, `industry`, `numemployees`).
  **Import this first.**
- `hubspot_contacts.csv` -- one row per contact, Contact-object properties
  only (no `industry`/`numemployees`), associated to its Company by
  `company_domain`. **Import this second.** `hubspot_owner_email` is an
  import-time lookup column only -- the property HubSpot persists is
  `hubspot_owner_id`, resolved from this email at import time.
  `hubspot_contact_id` is a demo join key for reconciling a run against
  `dedupe_report.json`; it is never sent to HubSpot as a Contact property.
- `dedupe_report.json` -- fuzzy-match method, threshold, matched pairs, fake
  rows excluded. With `--live`/`--live-dry-run` and at least one gray-zone
  pair, also carries `gray_zone_adjudication`: pairs in band, pairs
  evaluated/skipped (cap), merged/no_merge counts, parse failures, and the
  per-pair decision + rationale (dry-run: the exact prompt instead).
- `quality_report.json` -- per-field completeness, overall completeness vs.
  the 90% bar, and `needs_review_count` / `needs_review_pct` (kept separate
  from completeness so a high completeness number can't quietly launder
  rows the classifier couldn't actually resolve), plus the raw/verified
  dual metric (`raw_fill` / `verified_fill` top-level, and
  `spec_completeness.{contact,company}.spec_completeness_raw_pct` /
  `spec_completeness_verified_pct` / `fields_verified` /
  `synthetic_or_fallback_fields`) and `needs_review_broadened_count` /
  `_pct` -- see "Completeness -- two numbers, not one" above for what's
  excluded and the actual numbers on the offline fixture. Also carries
  `suppressed_count` / `mailable_count` / `suppressed` (judge fix #4): rows
  whose email domain is the host company's own or a named competitor
  (`config/icp.yaml`'s `suppression` key), flagged via each row's
  `suppression_reason` column in `hubspot_ready.csv`. Suppression only ever
  gates the mail send (M2 reads this column to build its recipient list,
  see `modules/m2-comms/README.md`) -- a suppressed row is still a full
  Contact in every CRM export here, never dropped from the CRM.
- `enriched.json` -- JSON mirror of `hubspot_ready.csv` for downstream
  modules (M2/M3/M4's assumed contract).
- `live_inference_report.json` -- only written with `--live` /
  `--live-dry-run`: batch counts, rows flagged/patched, parse-failure
  counts, and (dry-run only) every prompt sent.

Demo lane (`enrich.py`, this directory): stdlib-only rule tables + lookup tables + `--live`'s `claude -p` second opinion, honestly labeled per lane above.

Production lane: `clay_spec.md` -- same column-by-column waterfall, run in Clay against real HubSpot/Clearbit/LinkedIn enrichment APIs, with `prompts/inference.md` and `prompts/icp_scoring.md` as the LLM columns for the fields no lookup table can resolve.

### Clay lane (optional, `--clay-max`)

Default is **zero Clay calls** -- `--clay-max` defaults to `0` and nothing in `tools/clay_enrich.py` runs unless you opt in. Enable with `--live --clay-max N` (N > 0) to backfill industry/`numemployees`/country on up to N distinct company domains still missing/low-confidence after inference, via Clay's real "Enrich Company" function (`enrich_domains()` in `tools/clay_enrich.py`, respects the `CLAY_BIN` env var); use `--clay-dry-run` first to preview the exact domains with zero network calls. Every real run's call count and credit balance before/after are logged into `quality_report.json`'s `"clay"` key.

`data/incoming/registrants.csv`'s company domains are entirely synthetic (`acmerevenue.example`, `northfielddata.com`, ...) -- Clay returns nothing for a domain that doesn't exist, so `--clay-max` against that fixture would burn credits for zero rows back. Two separate live receipts exist for this reason, see `clay_spec.md`'s "Live proof" section for the full writeup:
- `out/clay-live-proof/clay_enrich_results.json` -- 3 real vendor domains via `tools/clay_enrich.py`'s own CLI; proves the raw Clay call/auth/routine ID.
- `modules/m1-enrichment/fixtures/clay_real_domains_registrants.csv` -- an 8-real-domain (Stripe, Notion, Figma, Airtable, Brex, Retool, Webflow, Linear) registrant-shaped fixture; run it with `--in modules/m1-enrichment/fixtures/clay_real_domains_registrants.csv --live --clay-max 8` (`--clay-dry-run` first, zero cost) to prove `enrich.py`'s own domain-selection + merge-back path end-to-end -- Clay's real industry/`numemployees`/country land in `hubspot_ready.csv` and `hubspot_companies.csv`, not only in a log.

### Speakers (`--speakers` / `--segments`)

Speakers never appear in the registrant CSV. `data/incoming/speakers.json` has name/title/company/bio but no email; `data/fixtures/segments.json`'s `speakers` key is a flat email list with no name -- `load_speakers()` pairs the two (matching each speaker's `firstname.lastname` localpart against the segment email list, not a fragile array-index assumption) and appends them to the same prepped-row pipeline a registrant goes through, so a speaker gets identical rule-engine treatment: dedupe, industry/function/seniority classification, ICP tier, HubSpot create/update, and host-domain suppression (an internal speaker on the host's own domain is suppressed from M2's mailable set exactly like an internal registrant would be). The one deliberate difference: `lifecyclestage` is set to `evangelist` rather than run through the attendee attended/session-length rubric, which has no meaning for a speaker -- `evangelist` is HubSpot's own top-ranked lifecycle stage (see `LIFECYCLE_RANK`), the correct semantic for someone who publicly presented for the host rather than a funnel prospect being nurtured. Defaults to `data/incoming/speakers.json` / `data/fixtures/segments.json`; override with `--speakers` / `--segments` for a different event. A missing file or an unmatched speaker degrades to a warning, never a crash.
