# HubSpot Wiring — M2 Post-Event Comms (production send path)

`comms.py` never calls a real API — it stops at `emails/*.md` + `sends_log.json` +
`approval_gate.json`, all offline, all stdlib. This doc traces what actually fires
once a human approves those files, verified end to end against the real script
(`modules/m1-enrichment/push_to_hubspot.py`, M1's file — not owned by this module,
described here read-only) with `--dry-run` (zero network calls, every request body
inspected) on 2026-08-24. No live token was used to write or verify this doc.

## 0. The honest claim (read this before the rest)

SPEC.md M2 says two things: "dispatched within 24 hours" and "logged to HubSpot
with UTM tracking." This build does **not** dispatch anything — no code path in
this repo, anywhere, calls an email-sending API (HubSpot's transactional
single-send, SMTP, or otherwise). That is a deliberate, documented boundary: the
human-approval gate exists precisely because a bad send is external-facing and
irreversible, and nothing downstream of it has ever run end to end before this
pass. What genuinely closes is the second clause: once a human flips
`approval_gate.json`, `push_to_hubspot.py --log-emails` writes one real HubSpot
CRM **Email engagement** (`POST /crm/v3/objects/emails`) per contact, associated
to that contact's timeline, carrying the exact subject line and UTM-tagged
recording link that would have gone out. That is a logged historical record of a
send, not a live send — HubSpot's own transactional/marketing send APIs are never
called. Precision matters here: "logged to HubSpot" is real and provable below;
"dispatched" is not, and no wording in this repo should imply otherwise.

## 1. Auth

- HubSpot private app token, scope `crm.objects.contacts.read/write` (for the
  properties/upsert/associate steps) + whatever scope `/crm/v3/objects/emails`
  needs for a create (standard CRM engagement write, covered by the portal's
  provisioned `timeline` scope — see BUILD-BRIEF's live-accounts list).
  `push_to_hubspot.py` never calls HubSpot's `transactional-email` single-send
  API — see §0 — so that scope isn't exercised by this repo even though the
  portal has it.
- Token: env var **`HUBSPOT_TOKEN`** (not `HUBSPOT_PRIVATE_APP_TOKEN` — an
  earlier draft of this doc named the wrong var; corrected 2026-08-24 against
  `push_to_hubspot.py::resolve_token()`), else a `HUBSPOT_TOKEN=...` line in
  `~/.config/postevent/hubspot.env`. Never committed, never read by this module
  (`comms.py` has no HubSpot code at all — it only ever writes the two gate
  files `push_to_hubspot.py` later reads).

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
  for speakers) custom properties, keyed off `function` / `name`. That sync job is
  **not built** — `push_to_hubspot.py --log-emails` reads these values out of
  `sends_log.json` directly to compose the engagement's `hs_email_text` (see §5),
  it does not write them onto the contact as custom properties. If a future pass
  wants the properties on the contact record itself (not just in the logged
  engagement body), that's still a single CRM batch update per segment, unbuilt.
- `{{unsubscribe_link}}` is left untouched deliberately — HubSpot injects its own
  native subscription-management token; do not override it.
- `{{recording_cta}}` / `{{recording_cta_timestamped}}`, `{{recording_link}}`,
  and (no-show template only) `{{event_time_since_close}}` and
  `{{function_relevant_segment}}` are **not** HubSpot tokens at all —
  `comms.py::run()` resolves each to plain text (UTM-tagged link, elapsed
  days since `event.json`'s date, and the timestamp `resolve_takeaway()`'s
  `general`-function takeaway cites) before the template is ever written to
  `emails/*.md`. Nothing needs a matching custom property for these; if you
  see one of these four tokens still literal in a rendered `.md`, that's a
  bug in `comms.py`, not a missing HubSpot property.

## 3. What actually fires after `approval_gate.json` flips (traced against the real code)

`push_to_hubspot.py` always runs its steps in this fixed order in one process —
there is no way to invoke just step 5 in isolation against an already-populated
portal; `--log-emails` piggybacks on the contact IDs this same run just upserted:

1. **ensure-properties** — `POST /crm/v3/properties/{contacts|companies}` for
   6 contact + 1 company custom property (409 already-exists = success).
2. **upsert companies** — search-by-domain, then `batch/update` matches +
   `batch/create` the rest (HubSpot won't `batch/upsert` on `domain`, it isn't
   unique-indexed — M1 lane's fix, not this module's).
3. **upsert contacts** — `POST /crm/v3/objects/contacts/batch/upsert`,
   `idProperty=email`, from M1's `hubspot_contacts.csv` (133 rows this event:
   131 mailable + 2 suppressed, still CRM records, just never mailed — see
   `README.md`'s "Who gets mailed").
4. **associate** — `POST /crm/v4/associations/contacts/companies/batch/create`
   using the object IDs steps 2–3 just returned.
5. **`--log-emails`** (only if the flag is passed): reads `m2/sends_log.json`
   and `m2/approval_gate.json` from **this run's `--in` dir**. Checks the gate
   first (`approved: true` **and** `status: "approved"` as literal JSON — a
   string `"true"` does not satisfy Python's `is True` check and will leave the
   gate reading as closed; see the exact command in §7). If closed: prints why
   and no-ops, `status: "skipped"`, zero requests. If open: for every
   `sample_sends[]` entry in every batch, builds one `POST
   /crm/v3/objects/emails` body (`hs_email_subject` = the batch's `subject_a`,
   `hs_email_text` = a rendered preview of `takeaway_headline`/`takeaway_body`
   or `top_quote` + the UTM-tagged recording link, `hs_timestamp` = now) and
   associates it to the contact ID this run's step 3 resolved for that email.
   A contact with no ID from step 3 is silently counted in
   `skipped_no_contact_id`, not sent — see §6, this is exactly the speaker gap.
6. **`--verify`** (only if passed): searches contacts/companies by `event_tag`
   and prints totals, plus an unfiltered first-100 sample of `/crm/v3/objects/emails`
   (that object has no `event_tag` property, so it can't be filtered the same way).

`sample_sends[]` (as of 2026-08-24, this module's fix): **every** mailable
attendee/no-show contact, not a preview slice — 72 attendee + 59 no-show + 3
speaker = 134 entries for this event, matching `to_count` exactly. Verified:

```
attendee to_count=72 sample_sends_len=72
no_show  to_count=59 sample_sends_len=59
speaker  to_count=3  sample_sends_len=3
```

(Field name `sample_sends` is kept as-is even though it's now the full list —
`push_to_hubspot.py::build_email_log_entries` reads that literal key; renaming
it would be a cross-module contract change this lane doesn't own.)

## 4. Tracking

- UTM convention (`utm_source=webinar&utm_medium=email&utm_campaign=<event-slug>&utm_content=<variant>`)
  is baked into every recording link `comms.py` writes — both the frontmatter
  (`recording_link_variant_a/b`) and every inline markdown link in the body,
  verified identical for a real 2026-08-24 offline run:
  `https://drive.example/rec/pipeline-after-webinar.mp4?utm_source=webinar&utm_medium=email&utm_campaign=pipeline-after-the-webinar-2026-07-20&utm_content=attendee-thank-you-a`
  — same `utm_campaign` across all three segments (attendee/no_show/speaker),
  six distinct `utm_content` values (one per segment × A/B variant), so every
  link rolls up to one campaign while staying distinguishable by segment+variant.
- HubSpot's own click/open tracking runs on top of that automatically for any
  link inside a marketing email; the UTM params on the destination URL are what
  let downstream web analytics (GA4, the destination site) attribute back to this
  exact campaign + variant, independent of HubSpot.
- Every send should be associated with a HubSpot **campaign** object named after
  `utm_campaign` (`pipeline-after-the-webinar-2026-07-20`) — that's the single
  rollup ID Sara Alvarez's attribution point in the transcript ([50:50]) depends on:
  "every downstream asset ... rolls up to that same campaign ID." Creating that
  campaign object and associating the logged emails to it is **not built** —
  `run_log_emails()` creates the `/crm/v3/objects/emails` engagement and
  associates it to the contact only, not to a campaign object. A production pass
  would add one `POST /crm/v3/objects/campaigns` (or reuse an existing one keyed
  on the same slug) plus a campaign association per logged email.

## 5. Logging — what "logged to HubSpot" actually means here

`sends_log.json`'s `sample_sends[]` entries are HubSpot single-send-API-shaped
(`message.to`, `contactProperties`, `customProperties`) because that's the
easiest shape to eyeball for a human reviewer approving the batch — but
`push_to_hubspot.py --log-emails` does **not** call that API. It transforms each
entry into a CRM **Email engagement** create (`/crm/v3/objects/emails`,
`hs_email_direction: EMAIL`, `hs_email_status: SENT`) associated to the contact
via association type 198 (`email -> contact`, HubSpot-defined). That's a
timeline record — visible on the contact's record as "an email was sent" — not
a trigger that causes an email to leave HubSpot. §0 above is the precise
boundary; this section is the mechanism.

## 6. Known gap — speaker segment never gets logged in a live run

**Update (2026-08-24, M1 lane): both fixes below are now built.** `enrich.py`
folds `segments.json`'s speakers into `hubspot_contacts.csv` by default (commit
`05a826f`, `load_speakers()`), and `push_to_hubspot.py --include-speakers` now
also upserts the 3 speakers directly from `data/incoming/speakers.json` +
`data/fixtures/segments.json` as a belt-and-suspenders path for `--in` dirs
generated before that commit (see `HUBSPOT_PUSH.md`). The `out/verify-m2/`
run below was generated against a working-tree state that predated
`05a826f` reaching this checkout, which is why it shows the gap; a fresh run
against current `HEAD` does not (`out/pipeline-after-the-webinar-.../m1/hubspot_contacts.csv`
already has 138 rows, all 3 speakers included). **This closes the *wiring*
gap** — `push_to_hubspot.py`'s `email_id_map` now gets a real upsert attempt
for all 3 speaker emails either way, not zero.

**It does not close the gap end to end, though** — `push_to_hubspot.py`'s own
`salvage_failed_chunk()` docstring documents a live-observed fact that no
contact-upsert code path can work around: HubSpot rejects the 3 speakers'
`.example`-TLD addresses as `INVALID_EMAIL`. So on a real live push these 3
contacts still never get an id, and `--log-emails` will still report them
under `skipped_no_contact_id` — now surfaced by name with HubSpot's own
rejection reason (`contact_rejected`) instead of silently dropped, but not
actually logged. That is a HubSpot-side validation call, not a
missing-contact-record bug — the only real fix is a non-`.example` speaker
address; `skipped_no_contact_id=3` stays the *known-good* live outcome (see §7).

Original finding, kept for the record (verified against a real M1 run on
2026-08-24, `out/verify-m2/m1/hubspot_contacts.csv`, gitignored scratch,
reproduce with the commands in §7, run against a working-tree state that
predated `05a826f`): **none of the 3 speaker emails appeared in
`hubspot_contacts.csv`** at that point, because `enrich.py` had not yet been
extended to read `segments.json`'s speaker list.

Consequence (as it stood before the M1-lane fixes above): `push_to_hubspot.py`'s
`email_id_map` (built from *this run's own* contact-upsert responses) had no
entry for any speaker email, so all 3 speaker `sample_sends` entries resolved
to `skipped_no_contact_id` in a **live** run — they silently did not get
logged, even though the gate was open and the wiring otherwise worked.
`--dry-run` could not surface this: dry-run mode uses the raw email string as
a placeholder contact ID (`contact_id = email if dry_run else
email_id_map.get(email)`), so the dry-run receipt in §3 showed all 134
entries as "planned" — 131 of those were real, 3 were not.

This was flagged in the sends_log itself (`comms.py`'s speaker batch carries a
`hubspot_log_emails_note` field explaining this, an extra key `push_to_hubspot.py`
ignores) rather than left silent.

## 7. Handoff — exact commands for Kai (needs the live token; not run by this pass)

```bash
# 1. Generate real output (already done for this event; re-run for a new one):
python3 modules/m1-enrichment/enrich.py --in data/incoming/registrants.csv --out out/<slug>/m1
python3 modules/m2-comms/comms.py --out out/<slug>/m2 --enriched out/<slug>/m1/hubspot_ready.csv

# 2. Review out/<slug>/m2/emails/*.md, then flip the gate -- literal JSON booleans,
#    not strings (see the is-True gotcha in §3 step 5):
python3 - <<'PY'
import json, pathlib
p = pathlib.Path("out/<slug>/m2/approval_gate.json")
d = json.loads(p.read_text())
d["approved"] = True
d["status"] = "approved"
p.write_text(json.dumps(d, indent=2))
PY

# 3. Push + log + verify against the real sandbox (token from ~/.config/postevent/hubspot.env):
python3 modules/m1-enrichment/push_to_hubspot.py --in out/<slug> --log-emails --verify
```

**What success looks like:** the `log-emails` receipt line reads
`status=ok` (or `partial` — expect the 3-speaker gap from §6, i.e.
`skipped_no_contact_id=3` is the *known-good* outcome right now, not a
failure), `requests=131 records=131`. The `verify` line's `contacts=` and
`companies=` counts should read `133` and `30` respectively for this event —
the exact numbers this pass's own `--dry-run` (§3) plans against
`out/verify-m2/m1/hubspot_{contacts,companies}.csv` (gitignored scratch,
reproduce with the enrich.py command in step 1 above). To confirm the engagements
actually landed, the sandbox UI (portal 247135551) → any mailed contact →
timeline should show an "Email" activity with today's timestamp, the real
subject line, and the UTM-tagged recording link in the body — that's the
receipt a screenshot should capture, since `/crm/v3/objects/emails` has no
`event_tag` property to filter a bulk read-back on (see §3 step 6's `--verify`
note; it falls back to an unfiltered first-100-page sample, not a precise
count for this event).

If `--log-emails` reports `status=failed` with 401s, the token in
`~/.config/postevent/hubspot.env` is the first thing to check — see §1 for the
correct variable name.
