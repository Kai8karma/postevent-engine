# Operating Cadence

How this actually runs week to week, once it's live — written for the
person operating it, not the person pitching it. No code in this file; every
claim is either a policy decision or a plain statement of what exists today
vs. what doesn't.

## The weekly cycle

One event = one run. There's no scheduler in this build — a human (or an
n8n webhook trigger, in the cloud lane) kicks off
`orchestrator/run_pipeline.py --live` once the transcript and registrant
export for that week's event are in `data/incoming/`. From there:

1. **M1 runs first, unattended.** Dedupe, enrichment, ICP tiering,
   HubSpot push. Takes minutes, not hours — no human step inside it.
2. **Review queue, same day.** See "Who owns the review queue" below.
3. **M2 renders, then stops at the approval gate.** No email leaves this
   system until a human flips `approved:true`.
4. **M3 runs independently of M1/M2's gate** — content repurposing doesn't
   block on comms approval, and vice versa.
5. **M4 dashboard is live the moment M1 has written its output** — it reads
   M1's files directly, not HubSpot, so it doesn't wait on the push.

There is no cross-event step in this cycle today — each run is scoped to
one event's own contacts and one event's own sends. That's a deliberate
limit, not an oversight; see "What breaks at 10x" below.

## Who owns the review queue

Two separate queues, two separate owners, and they should not be collapsed
into one person's job:

- **M1's `needs_review` rows** (filter `hubspot_ready.csv` on
  `needs_review=true`, or read `quality_report.json`'s
  `needs_review_count`/`needs_review_pct`) are a **data-quality** queue —
  today that's exactly one condition: a company name the offline industry
  classifier couldn't confidently place (non-ASCII-dominant name, fell
  through to `Other`). Owner: whoever owns CRM data hygiene (ops/RevOps),
  not the SDR. Action: confirm or correct the industry, clear the flag by
  hand or re-run with `--live` (the LLM classifier resolves more of these
  than the offline keyword table does).
- **M2's `approval_gate.json`** (`status: pending_human_approval` until set
  otherwise) is a **content/compliance** queue — did the AI-generated
  copy say something wrong, off-brand, or non-compliant before it goes to
  a real inbox. Owner: whoever owns outbound comms (marketing/demand-gen
  lead), not ops. Action: read the rendered `emails/` output for all three
  segments (attendee/no-show/speaker), then flip `approved:true` — nothing
  auto-sends on any other condition.

Neither queue self-clears on a timer. If nobody looks at `approval_gate.json`,
sends sit at `pending_human_approval` indefinitely — that's intentional
(no email should go out because a deadline passed), but it means the queue
needs an owner with an actual SLA, not just a JSON file nobody's watching.

## SLA: same-day SDR handoff

The brief's own spec line: *"Ready for SDR handoff same day"*
(`SPEC.md`). What that means operationally: M1's push into HubSpot
(contact + company completeness above 90%, ICP tier + rationale, region,
owner, lifecycle stage — all set per row) should land the same calendar day
the event happens, so a tier1/tier2 lead assigned to an SDR is in their
queue, correctly routed, before the next business day starts. This build
meets that mechanically — M1's live run against the fixture completes in
well under an hour end to end, HubSpot push included — but "same day" is
only true in practice if the data-quality review queue above doesn't sit
unworked. A `needs_review` row that's still flagged when the SDR picks up
the lead is a same-day miss even though the pipeline ran on time.

## What breaks at 10x (≈40 events/year)

Said plainly, because it doesn't exist today and pretending otherwise would
be the wrong kind of confident:

- **No cross-event contact ledger.** Every run's dedupe (§2 of
  [`data-contract.md`](data-contract.md)) matches within-batch and against
  HubSpot, but there's no local record of "this person already came through
  event #6" independent of whatever's already landed in HubSpot. At one
  event a month this is invisible — HubSpot itself is the de facto ledger,
  and the live-matching step covers it. At 40 events a year, with re-orgs,
  new emails from job changes, and CRM-side merges happening between runs,
  the gap between "matched against HubSpot's current state" and "matched
  against everyone this system has ever seen" starts to matter, and nothing
  here closes it.
- **No send-fatigue suppression.** M2 gates every send behind human
  approval, but that gate is per-event, per-run — it has no memory of how
  many other emails this same contact received from other events this
  month. A contact who attends four webinars in a quarter gets four
  independently-approved sends with no shared throttle across them. At one
  event a month a human approver can hold that context in their head. At
  40 events a year, they can't, and this system gives them no help doing
  it — that's a real gap, not a nice-to-have.

Both are cross-event, stateful problems; this build is intentionally
single-event-scoped (see "The weekly cycle" above), so neither is a bug in
what exists — they're the honest boundary of what exists.

## What to deprecate when HubSpot ships it natively

Three pieces of this build exist only because HubSpot doesn't do them out of
the box today. The day any one of them ships as a native HubSpot feature,
cut it here rather than maintaining a parallel implementation:

- **Fuzzy dedupe against existing contacts** (§2 of the data contract) — if
  HubSpot's own duplicate-management ever does cross-field fuzzy matching
  (name + company, not just exact email) at upsert time instead of only in
  its separate dedupe tooling, M1's `dedupe_against_hubspot` step becomes
  redundant and should be cut, keeping only the within-batch pass (which
  still has to happen before any HubSpot call at all).
- **The lifecycle no-regression check** (§4 of the data contract) — this
  exists because HubSpot's own batch upsert will happily overwrite a
  contact's `lifecyclestage` with whatever's in the payload, including
  backward. If HubSpot ever ships a native "never demote lifecycle stage"
  write option, `enrich.py`'s manual `LIFECYCLE_RANK` comparison goes away.
- **The industry-enum mapping table** (`HUBSPOT_INDUSTRY_ENUM`, §6 of the
  data contract) — this exists because the Company `industry` property is a
  fixed enum with no free-text or custom-value option today. If HubSpot
  ever opens that property to custom values (or ships a public mapping
  API), the hand-maintained table in `push_to_hubspot.py` becomes dead code.

What does **not** get deprecated by a HubSpot feature ship: the ICP tier
rubric, the lifecycle-stage rubric, and the region/owner routing — those
encode this specific company's go-to-market judgment, not a CRM plumbing
gap, and no CRM vendor is going to ship "know this company's ICP" as a
feature.
