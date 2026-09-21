# Operating Cadence

How this actually runs week to week, once it's live — written for the
person operating it, not the person pitching it. No code in this file; every
claim is either a policy decision or a plain statement of what exists today
vs. what doesn't.

## The weekly cycle

One event = one run. In v2 the trigger is n8n, not a person running a CLI
command by hand: M1, M2 and M3 each fire off their own webhook
(`POST /webhook/m1-run` etc. — see
[`orchestrator/n8n/railway/README.md`](../orchestrator/n8n/railway/README.md))
once that week's registrant export and transcript land in `data/incoming/`
(or a per-event dir); M4 runs on a 6-hour schedule trigger plus one manual
`seed` run right after M1's push. The local one-command equivalent for
someone without n8n access is `python3 orchestrator/run_pipeline.py` — live
by default now, `--offline` opts out, `--modules m1,m4` runs a subset (see
[`orchestrator/README.md`](../orchestrator/README.md)).

1. **M1 runs first, unattended.** Dedupe, enrichment, ICP tiering —
   `finalize` pushes the result into HubSpot as its last step, same run.
2. **Review queue, same day.** See "Who owns the review queue" below.
3. **M2 generates, then stops at the approval gate.** n8n's Wait node holds
   the run until a human calls the `approve_url` it returns; no email
   leaves the system before that.
4. **M3 runs independently of M1/M2's gate** — content repurposing doesn't
   block on comms approval, and vice versa.
5. **M4 needs M1's push and M2's logged sends already in HubSpot before it
   means anything.** It seeds this event's engagement stream into HubSpot
   itself (once, by hand, `seed:true`), then every scheduled run reads the
   portal back — it does not read M1's local output files directly; HubSpot
   is the source of truth (see `docs/module-api.md`'s M4 table).

There is no cross-event step in this cycle today — each run is scoped to
one event's own contacts and one event's own sends. That's a deliberate
limit; see "What breaks at 10x" below.

## Who owns the review queue

Two separate queues, two separate owners, and they should not be collapsed
into one person's job:

- **M1's `needs_review` rows** (filter `hubspot_ready.csv` on
  `needs_review=true`, or read `quality_report.json`'s
  `needs_review_count`/`needs_review_pct`) are a **data-quality** queue —
  today that's two conditions: a company name the offline rule cascade
  couldn't confidently place (`needs_review_reason=non_ascii_company_
  unclassified`), and the live LLM's ICP-tier score landing more than one
  level from the rule engine's tier (`icp_disagreement`). Owner: whoever
  owns CRM data hygiene (ops/RevOps), not the SDR. Action: confirm or
  correct and clear the flag by hand.
- **M2's `approval_gate.json`** (`status: pending_human_approval` until set
  otherwise) is a **content/compliance** queue — did the AI-generated copy
  say something wrong, off-brand, or non-compliant before it goes to a real
  inbox. Owner: whoever owns outbound comms (marketing/demand-gen lead),
  not ops. Action: read the three rendered variants, then approve via the
  `approve_url` n8n's Wait node returned — nothing auto-sends on any other
  condition, and that Wait node times out at 24h if nobody acts.

Neither queue self-clears on a timer beyond that 24h Wait-node limit. If
nobody looks at `approval_gate.json`, that run's sends simply never happen
— intentional (no email should go out because a deadline passed), but it
means the queue needs an owner with an actual SLA.

## SLA: same-day SDR handoff

The brief's own spec line: *"Ready for SDR handoff same day."*
Operationally: M1's `finalize` phase (contact + company completeness ≥90%
verified, ICP tier + rationale, region, owner, lifecycle stage — all set
per row) should complete the same calendar day the event happens, so a
tier1/tier2 lead is in an SDR's queue, correctly routed, before the next
business day starts. This build meets that mechanically — the HubSpot push
is the last step inside `finalize` itself, not a separate later job — but
"same day" is only true in practice if the data-quality review queue above
doesn't sit unworked, and if the contact's country resolved to an owner at
all (see `data-contract.md` §5 — a registrant whose country only matches
the legacy fallback region table gets no `hubspot_owner_email`, not a
defaulted one). A `needs_review` row, or an unrouted owner, still open when
the SDR picks up the lead is a same-day miss even though the pipeline ran
on time.

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
  its separate dedupe tooling, M1's `dedupe_against_hubspot()` step becomes
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
encode Darwinbox's own go-to-market judgment, not a CRM plumbing gap, and
no CRM vendor is going to ship "know this company's ICP" as a feature.
