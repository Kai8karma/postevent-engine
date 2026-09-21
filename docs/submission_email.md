---
status: draft
drafted_at: 2026-09-21
to: "Mehndi Zaveri (Darwinbox) — reply in-thread to her assignment email"
subject: "Revised submission — AI-led post-event automation (Kshitij Mishra)"
note: >-
  Supersedes the 2026-08-23 email (see git history for that version). That
  submission did not follow the case study brief; this is the rebuild. The
  2026-08-23 email also wrongly claimed to have been drafted by Module 2's
  own comms pipeline -- it was hand-written, same as this one.
---

Hi Mehndi,

Following up on the feedback that the first submission didn't follow the
case study — this is a rebuild against the brief's four modules directly,
not a repeat of the earlier pitch.

What it does, module by module:

- **M1 (lead enrichment)**: dedupes registrants against HubSpot (fuzzy and
  exact), infers title/function/seniority/industry/company size, scores ICP
  fit against Darwinbox's own buyer profile, and pushes the result into
  HubSpot as contacts, companies and associations.
- **M2 (post-event comms)**: drafts three segment-specific emails (attendee,
  no-show, speaker) from the real event transcript, checks every claim in
  them back against the transcript, holds them behind a human approval
  step, and logs each send into HubSpot as an email engagement. The
  dispatch step itself runs in the n8n workflow — see the note on what
  hasn't run yet, below.
- **M3 (content repurposing)**: turns the same transcript into a blog
  draft, YouTube chapters and description, an infographic outline, and
  social posts — every quote and timestamp is checked against the
  transcript before an asset ships.
- **M4 (lead intelligence dashboard)**: reads the contacts and engagement
  history back out of HubSpot, layers an AI-scored read (anomalies, lead
  interest, stage-movement narrative, buying committees) over a
  deterministic validator, and renders a dashboard whose narrative
  refreshes on load.

All four are wired together by n8n workflows calling a small API in front
of the underlying Python engine — n8n owns the trigger, the approval wait,
and the send step; the API owns the enrichment/generation/analysis logic
itself.

What's real and what's simulated, plainly, since that distinction matters
more than the pitch:

- The event is real — a public Darwinbox webinar ("From Hype to
  High-Impact: How to Start and Scale AI in HR") — and the recording, the
  Sarvam speech-to-text transcript, and everything generated from that
  transcript are real.
- The registrant list is generated, not real: no shareable attendee list
  exists for this webinar, so the 150 registrants are synthetic people at
  real companies (real employer domains, so firmographic lookups return
  real data).
- The post-event engagement stream (opens, clicks, pageviews, form fills)
  is seeded into a HubSpot developer test portal for this demo, and
  labelled `source: seeded` everywhere it shows up — in the underlying
  data and on the dashboard itself.
- Images (thumbnail and social visuals) didn't ship this round — the
  image-generation API returned HTTP 402 on a key with no funded credits.

What hasn't run yet, so you don't have to take the above on trust: the four
n8n workflows are written and audited but have not been imported into a
running n8n instance, so no email has actually been dispatched end to end
and there's no master execution log yet. The module API runs locally and in
a container but isn't hosted, and three HubSpot scopes (marketing email,
transactional email, custom behavioural events) return 403 on this portal,
which is why the engagement stream is written to contact properties rather
than as custom events.

Links, filled in once each one resolves rather than guessed at now:
repo — [PLACEHOLDER: GitHub URL]. ZIP — [PLACEHOLDER: attached or hosted
link]. Hosted console and dashboard — [PLACEHOLDER: URL]. I won't send this
until those are live.

Happy to walk through any of the four modules directly.

Kshitij Mishra
