# M2 — Post-Event Communications

Renders the attendee / no-show / speaker thank-you emails and gates every send behind human approval.

- `python3 comms.py --out <dir>` — offline, reads cached `sample_output/*.json` takeaways.
- `python3 comms.py --out <dir> --enriched <hubspot_ready.csv|.json> --live` — live copy via `claude -p`.
- Falls back to `data/incoming/registrants.csv` when `--enriched` is missing (M1 not run yet) -- this is a *personalization* fallback only (job title/company for the copy). See the recipient-list rule below, which is separate and stricter.
- Output: `emails/*.md` (3 variants, UTM-tagged), `sends_log.json` (HubSpot-shaped -- `sample_sends[]` per segment covers every mailable contact, not a preview slice, so "all logged to HubSpot" is literally true once approved and pushed), `approval_gate.json` (blocks all sends until a human sets `approved: true`). Nothing in this module ever calls a send API -- see `hubspot_wiring.md` for what fires downstream of the gate and exactly what "logged" does and doesn't mean.

## Who gets mailed (judge fix #1 + #4)

The attendee/no-show recipient list is **not** `data/fixtures/segments.json`'s
raw registrant lists by default -- it is derived from M1's `--enriched`
output (`segment_from_enriched()` in `comms.py`), keyed on that file's
`attendance_status` column. Two consequences fall out of that on purpose:

1. **Duplicate registrants are mailed once, not per address.** M1's fuzzy
   dedupe (`enrich.py::dedupe_within_batch`) collapses a same-person cluster
   to one surviving primary row before `hubspot_ready.csv` is ever written --
   the merged-away duplicate email(s) never get a row there. Since M2 now
   segments strictly off that row set, an alternate address that got merged
   away is never a recipient. **Deliberate choice: the alternates are
   suppressed, not multi-mailed** -- only the single surviving primary
   address gets the send. The alternate email is not silently lost, though:
   it's still recorded in M1's `dedupe_report.json` (`within_batch_duplicates`,
   `primary_email`/`duplicate_email` pairs) as the audit trail.
2. **Host/competitor domains are excluded from the mailable set, not from
   the CRM.** A row M1 flagged `suppression_reason` (host company staff or a
   named competitor domain, see `config/icp.yaml`'s `suppression` key) is
   still a Contact in `hubspot_ready.csv`/`hubspot_contacts.csv` -- it's just
   dropped from M2's send list. Counted in `comms.json`'s
   `recipient_pipeline.suppressed` and printed in the run summary.

**Fallback**: if `--enriched` is absent or its path doesn't exist, M2 falls
back to `segments.json`'s raw lists and prints a `WARNING` to stderr -- that
fallback list is undeduped and unsuppressed (M1 hasn't run, so there's
nothing to defer to). This should only happen when M1 hasn't run yet; in the
orchestrated pipeline (`orchestrator/run_pipeline.py`), M1 always runs
before M2 and `--enriched` is always passed.

Speakers are unaffected by any of this -- they're matched by name against
`event.json`'s named speakers via `match_speaker_emails()`, sourced from
`segments.json`'s `speakers` list (a separate, small, hand-curated list;
speakers are never in `registrants.csv` and so never flow through M1 at all).
**Consequence for HubSpot logging** (traced 2026-08-24, see `hubspot_wiring.md`
§6): because speakers never flow through M1, they're never in
`hubspot_contacts.csv` either, so `push_to_hubspot.py --log-emails` has no
contact ID to associate their engagement to -- their `sample_sends` entries
are built and flagged (`hubspot_log_emails_note`) but will not actually log in
a live run until M1's lane also upserts speakers as contacts.
- Prompts in `prompts/`, production send path in `hubspot_wiring.md`.
- `--live` LLM backend: `LLM_BACKEND` env selects `auto` (default, tries `claude -p` then falls back to OpenRouter if a key exists), `claude`, or `openrouter`.
- OpenRouter key: `OPENROUTER_API_KEY` env, else a `OPENROUTER_API_KEY=...` line in `~/.config/postevent/llm.env`; never printed.
- `OPENROUTER_MODEL` overrides the default model id (falls back through `anthropic/claude-sonnet-5` → `4.6` → `4.5` on 400/404 model errors).
