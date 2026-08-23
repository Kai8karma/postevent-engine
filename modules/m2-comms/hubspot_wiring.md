# HubSpot Wiring — M2 Post-Event Comms (production send path)

This module never calls a real send API — `comms.py` stops at `sends_log.json` +
`approval_gate.json`. This doc is the wiring a real HubSpot private app would need
to take those two files and actually dispatch. Two lanes, same as the rest of this
repo: **demo** (what runs today) and **production** (what plugs in when Kai's
HubSpot dev account is live).

## 1. Auth

- HubSpot private app, scope `transactional-email` (single-send) + `crm.objects.contacts.read/write`.
- Token in env var `HUBSPOT_PRIVATE_APP_TOKEN`, never committed. `hubspot_wiring.md`
  documents the shape; nothing in this repo reads that env var yet.

## 2. Templates and merge fields

Each file in `emails/*.md` is one HubSpot marketing email template, not a per-contact
copy. The frontmatter (`subject_a`/`subject_b`, `utm_campaign`, `utm_content_a/b`)
maps directly to:

- Two marketing email records (A/B), or one email with HubSpot's native A/B subject
  test enabled — either works, `utm_content_a`/`utm_content_b` already give you
  distinct tracking either way.
- Body tokens (`{{first_name}}`, `{{company}}`, `{{takeaway_headline}}`,
  `{{takeaway_body}}`, `{{top_quote}}`, `{{internal_or_external_note}}`) become
  HubSpot personalization tokens bound to **custom contact properties**, not
  hardcoded copy. M1's enrichment pass (or this module's fallback classifier) is
  what sets `function` and `icp_tier`; this module's cached `by_function` /
  `by_speaker` maps (`sample_output/*-takeaways.json`, `speaker-quotes.json`) are
  what a one-time sync job would write into each contact's
  `takeaway_headline` / `takeaway_body` (or `top_quote` / `internal_or_external_note`
  for speakers) custom properties, keyed off `function` / `name`. That sync is a
  single CRM batch update per segment (one API call class, not N).
- `{{unsubscribe_link}}` is left untouched deliberately — HubSpot injects its own
  native subscription-management token; do not override it.

## 3. Send flow

1. `comms.py` runs (offline or `--live`), writes `emails/`, `sends_log.json`,
   `approval_gate.json` — all three land in `--out`, nothing is sent.
2. A human opens `emails/*.md`, reviews the copy and the "Personalization preview" /
   "Rendered sends" section, and either edits the templates or approves as-is.
3. Human flips `approval_gate.json`: `"approved": true`, `"status": "approved"`.
4. Production job (n8n `HubSpot Send` node, or a small script) reads
   `approval_gate.json`; if not approved, it no-ops (`orchestrator/n8n/m2-comms.json`
   already gates on exactly this check).
5. Once approved, for each batch in `sends_log.json`:
   - Sync the batch's `by_function`/`by_speaker` copy into the relevant contacts'
     custom properties (one CRM batch update per segment).
   - Call HubSpot's marketing email **single-send API**
     (`POST /marketing/v3/transactional/single-email/send`) once per contact in the
     segment's HubSpot **active list** (built from `segments.json`'s attendee /
     no-show / speaker email lists), passing `emailId` for that segment/variant.
   - HubSpot resolves `{{first_name}}`, `{{takeaway_headline}}`, etc. per contact
     from the properties synced in the step above.

## 4. Tracking

- UTM convention (`utm_source=webinar&utm_medium=email&utm_campaign=<event-slug>&utm_content=<variant>`)
  is baked into every recording link `comms.py` writes — see
  `recording_link_variant_a/b` in each email's frontmatter.
- HubSpot's own click/open tracking runs on top of that automatically for any
  link inside a marketing email; the UTM params on the destination URL are what
  let downstream web analytics (GA4, the destination site) attribute back to this
  exact campaign + variant, independent of HubSpot.
- Every send should be associated with a HubSpot **campaign** object named after
  `utm_campaign` (`pipeline-after-the-webinar-2026-08-19`) — that's the single
  rollup ID Sara Alvarez's attribution point in the transcript ([50:50]) depends on:
  "every downstream asset ... rolls up to that same campaign ID."

## 5. Logging

`sends_log.json` is already shaped close to what the single-send API expects per
call (`message.to`, `contactProperties`, `customProperties`) — the production job's
job is to (a) expand each batch's sample to the full segment list, (b) call the API
per contact, (c) append the API's own `sendId`/status response back into a persisted
log (this file, or a HubSpot custom object) for audit. No send is fired without
step 3 above having happened first.

## 6. Rate limits / errors

HubSpot's transactional/single-send endpoints are rate-limited per app; batch
sends (150 attendees + 68 no-shows) should queue with backoff, not fire-and-forget
in a loop. Any 4xx/5xx from the API gets logged against that contact's row in the
send log with `status: "failed"` and is retried once before falling to a manual
review queue — never silently dropped.
