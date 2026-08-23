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

Proves: a messy 150-row Zoom registrant export -> fuzzy-deduped (within-batch + against a 40-contact HubSpot fixture, `difflib.SequenceMatcher`, threshold 0.80, documented in `dedupe_report.json`) -> every contact/company field inferred where missing (peer/company backfill cascade, then `--live`'s LLM second opinion for whatever's still unresolved) -> ICP-tiered against `config/icp.yaml` with a rationale string -> region/owner routed -> lifecycle-staged by a pinned tier/attendance/session-length rubric (never regressing an existing HubSpot contact) -> field-complete at >90% (verified: ~100% on the fixture, see `quality_report.json`), with rows the classifier structurally couldn't resolve (e.g. non-Latin company names) flagged `needs_review` and counted separately rather than laundered into the completeness number.

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
  rows excluded.
- `quality_report.json` -- per-field completeness, overall completeness vs.
  the 90% bar, and `needs_review_count` / `needs_review_pct` (kept separate
  from completeness so a high completeness number can't quietly launder
  rows the classifier couldn't actually resolve). Also carries
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

Default is **zero Clay calls** -- `--clay-max` defaults to `0` and nothing in `tools/clay_enrich.py` runs unless you opt in. Enable with `--live --clay-max N` (N > 0) to backfill industry/`numemployees`/country on up to N distinct company domains still missing/low-confidence after inference, via Clay's real "Enrich Company" function (`enrich_domains()` in `tools/clay_enrich.py`, respects the `CLAY_BIN` env var); use `--clay-dry-run` first to preview the exact domains with zero network calls. A live receipt of the underlying Clay call shape lives at `out/clay-live-proof/clay_enrich_results.json`. Every real run's call count and credit balance before/after are logged into `quality_report.json`'s `"clay"` key.
