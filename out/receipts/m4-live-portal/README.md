# M4 live run against the HubSpot test portal (2026-09-21, IST afternoon)

All four phases on the live lane against developer test portal 247135551, after the 30-row
M1 slice was pushed with `--event-tag darwinbox-ai-in-hr-2026-08-13`.

- `seed` — custom event definitions returned HTTP 403 on this portal, so the run fell back to
  contact properties (`postevent_opens/clicks/pageviews/form_fills`, `postevent_last_engaged`)
  and real `lifecyclestage` updates: 16 contacts matched, 73 engagement events, 12 lifecycle
  updates. See `receipts/m4_seed.json`.
- `sync` — pulled back 32 contacts and 23 companies, matching a direct HubSpot search of the
  same portal exactly, plus 1 email engagement logged earlier by M2. `events` is empty because
  the portal refuses custom event definitions; the counters carry the stream.
- `analyze` — 4 of 4 LLM calls on a free OpenRouter model. The deterministic math graded the
  model: 4 fields agreed, 3 disagreed, and every disagreement is recorded in
  `analysis.json.llm.validator` rather than being overwritten.
- `render` — `index.html`, numbers reconciling to `snapshot.json`.

## Why the movement numbers carry a disclosure

Every stage change in this portal was written by this pipeline: 32 by the M1 push and 7 by the
seed phase, all on 2026-09-21, plus one older row. HubSpot's property history reports them as
history, which is true but would read as organic movement over weeks. Rows written inside the
seed run are therefore labelled `seeded`, each window reports its timestamp dates, and the
dashboard card states how many transitions the pipeline wrote and that this is a seeded
developer test portal. The narrative says so in its first sentence.
