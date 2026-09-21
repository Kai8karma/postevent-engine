# Post-Event Engine — Darwinbox, "Start and Scale AI in HR"

A working post-event pipeline built against the brief in [`SPEC.md`](SPEC.md) and run end to end on **one real
event**: Darwinbox's own on-demand webinar *From Hype to High-Impact: How to Start and Scale AI in HR*
([public event page](https://explore.darwinbox.com/lp/resources/events/webinar-how-to-start-and-scale-ai-in-hr)),
33 minutes in three chapters, with **Q Hamirani** (Chief People Officer, HighLevel) and **Sudi Bjornstad Korba**
(SVP Sales North America, Darwinbox). The recording is public and hosted on Darwinbox's own HubSpot file CDN; the
transcript ([`data/incoming/transcript.md`](data/incoming/transcript.md)) came out of a real **Sarvam `saaras:v3`
batch STT** run over the three chapter audio files — no captions exist for this recording and none of it was typed
by hand (receipt: [`out/receipts/transcription.json`](out/receipts/transcription.json)). Four modules run off that
one event: **M1 Lead List Enrichment**, **M2 Post-Event Communications**, **M3 Content Repurposing**, **M4 Lead
Intelligence Dashboard**. Real model and API calls are the default lane in every module; `--offline` is an explicit
flag that replays this repo's checked-in output for a reviewer who has no keys.

## Two pages to read this from

Both are static HTML with no fetch calls and no server: open either file directly in a browser.

- [`docs/index.html`](docs/index.html) — **Control Room.** The fullest read: brief→artifact map, how the AI is the
  engine, real vs simulated, verified / not yet, the four modules, orchestration and how to run it.
- [`web/index.html`](web/index.html) — **Console.** The same story in short: the event, the map, AI-as-engine,
  real vs simulated, status.

Neither page regenerates anything. Every number on them is transcribed from the receipts in `out/receipts/`, which
are the authority if the two ever disagree.

## Brief → artifact map

| brief asks | what runs | where the output is | receipt |
|---|---|---|---|
| **M1 Lead List Enrichment** — dedupe/fuzzy-match against HubSpot, infer title/function/seniority/size/industry, enrich company data via Clay, score ICP fit; HubSpot-ready file >90% complete with tier, region, owner, lifecycle | [`modules/m1-enrichment/enrich.py`](modules/m1-enrichment/enrich.py) (live default): HubSpot Search dedupe → LLM gray-zone adjudication → LLM row inference + firmographics + ICP tier with rationale (rules validate) → `--clay-results` merge with per-field `*_source` (**wired, never executed** — no Clay run has happened; see *Not yet*) → [`push_to_hubspot.py`](modules/m1-enrichment/push_to_hubspot.py) upsert. n8n: [`m1-lead-enrichment.json`](orchestrator/n8n/railway/m1-lead-enrichment.json) | `<out>/hubspot_ready.csv`, `hubspot_contacts.csv`, `hubspot_companies.csv`, `quality_report.json`, `dedupe_report.json`, `live_inference_report.json`. The committed copies from the slice below are openable: [`hubspot_ready.csv`](out/receipts/m1-live-slice-30/outputs/hubspot_ready.csv), [`hubspot_contacts.csv`](out/receipts/m1-live-slice-30/outputs/hubspot_contacts.csv), [`hubspot_companies.csv`](out/receipts/m1-live-slice-30/outputs/hubspot_companies.csv), [`enriched.json`](out/receipts/m1-live-slice-30/outputs/enriched.json), [`dedupe_report.json`](out/receipts/m1-live-slice-30/outputs/dedupe_report.json), [`live_inference_report.json`](out/receipts/m1-live-slice-30/outputs/live_inference_report.json) | [`out/receipts/m1-live-slice-30/`](out/receipts/m1-live-slice-30/) — live slice of **32 rows** (the 30-row registrant sample plus the 2 speakers): **96.2% contact / 93.0% company verified completeness, PASS** at the 90% bar, 10 LLM calls attempted, 7 parsed cleanly, live HubSpot search. **In this slice the model scored no ICP tiers**: two ICP batches timed out against the free model's 90-second per-batch deadline, which retired that model for the run, so the third was never attempted (`icp_parse_failures: 3`, `icp_rows_scored_by_llm: 0`) and all 32 rows took their tier from the rule table (`icp_source: {rules: 32}`). The model's contribution to *this* receipt was dedupe adjudication and firmographic inference. Push: [`out/receipts/m1-push-slice30-2026-09-21.json`](out/receipts/m1-push-slice30-2026-09-21.json) — 32 contacts, 23 companies, 27 associations **as reported by the upsert calls**. That receipt's `verify` block reads `"status": "not_run"`, so these are the write's own counts, not an independent read-back |
| **M2 Post-Event Communications** — segment-specific copy, subject-line variants, takeaway snippets personalised by role/industry; n8n orchestration; HubSpot send + tracking; UTM on every link; three variants inside 24h of close | [`modules/m2-comms/comms.py`](modules/m2-comms/comms.py): one extraction call over the real transcript + one call per segment (attendee / no-show / speaker), per-recipient render off M1's `function`/`industry`, grounding gate, approval gate. Module API phases `generate`/`approve`/`log` ([`docs/module-api.md`](docs/module-api.md) §M2). n8n: [`m2-post-event-comms.json`](orchestrator/n8n/railway/m2-post-event-comms.json) | `<out>/dispatch_plan.json`, `emails/<segment>.md`, `approval_gate.json`, `extraction.json`, `grounding.json`. The committed copies are openable: [`dispatch_plan.json`](out/receipts/m2-live/outputs/dispatch_plan.json), [`approval_gate.json`](out/receipts/m2-live/outputs/approval_gate.json), [`extraction.json`](out/receipts/m2-live/outputs/extraction.json), [`emails/`](out/receipts/m2-live/outputs/emails/) (`attendee.md`, `no_show.md`, `speaker.md`) | [`out/receipts/m2-live/`](out/receipts/m2-live/) — `grounding.json` **81/81 checks passed, 0 failed**; `m2_llm_calls.json` 4 live calls, all HTTP 200 |
| **M3 Content Repurposing** — transcription API; extract moments/insights/quotes/data points; blog 800–1200 words, YouTube chapters + description + thumbnail brief, infographic outline, 5–10 social posts; image generation; saved to a shared drive, tagged by event | [`transcribe_batch.py`](modules/m3-repurpose/transcribe_batch.py) (Sarvam batch STT) → [`repurpose.py`](modules/m3-repurpose/repurpose.py): one extraction pass, every asset generated off it, [`verify_grounding.py`](modules/m3-repurpose/verify_grounding.py) hard gate with a repair pass → `make_clips.py` (ffmpeg, 16:9 + 9:16 + burned captions) → `gen_visuals.py` (image model). n8n: [`m3-content-repurposing.json`](orchestrator/n8n/railway/m3-content-repurposing.json) + the API's `record` phase for the Drive leg | `<out>/blog.md`, `youtube.md`, `infographic.md`, `social.md`, `extraction.json`, `grounding_report.json`, `manifest.json`, `clips/`, `visuals/`. In the committed package `clips/` holds the 3 `.srt` caption files and 3 `-thumb.png` stills only — **the six MP4s are not shipped**; `make_clips.py` regenerates them, but `data/incoming/media/` is gitignored and ships empty, so regenerating means re-downloading the three chapter files from the public CDN first | [`out/receipts/m3-live/`](out/receipts/m3-live/) — blog **1106 words**, 18 chapters, **8 posts** (5 LinkedIn + 3 X), grounding **46/46** — `youtube.md` 18 claims, `infographic.md` 12, `social.md` 9 and `blog.md` 7. **3 clips** cut in 2 ratios, with the SRTs and thumbnails committed and the MP4s regenerable, not shipped; `receipts/m3_images.json` records **0 of 3 images** (HTTP 402) |
| **M4 Lead Intelligence Dashboard** — HubSpot contacts, engagement events and lifecycle changes in; anomalies, lead-interest scores, narrated stage movement, top accounts and buying committees; HTML dashboard with attendee→MQL, 7/14/30-day movement, narrative refreshed on load | [`modules/m4-dashboard/dashboard.py`](modules/m4-dashboard/dashboard.py) `seed` → `sync` → `analyze` → `render`: writes the engagement stream and lifecycle changes into HubSpot, reads the portal back, LLM analysis checked by deterministic math, self-contained page; `GET /narrative/<run_id>?refresh=1` re-runs `analyze`. n8n: [`m4-lead-intelligence.json`](orchestrator/n8n/railway/m4-lead-intelligence.json) | `<out>/snapshot.json`, `analysis.json`, `dashboard_data.json`, `index.html` | [`out/receipts/m4-live-portal/`](out/receipts/m4-live-portal/) — all four phases live against the HubSpot test portal: `seed` fell back to contact properties after a 403 on custom event definitions (16 contacts matched, 73 engagement events, 12 lifecycle updates), `sync` read back 32 contacts + 23 companies over 6 calls all HTTP 200, `analyze` 4/4 completions with the validator logging **4 agreements and 3 disagreements**, nothing overwritten. A different, earlier fixture-snapshot analyze run sits alongside it — [`out/receipts/m4-live-analyze-fixture/`](out/receipts/m4-live-analyze-fixture/), 3 agreements and 4 disagreements — its numbers are not the portal run's |

## How the AI is the engine, not a wrapper

- **M1 dedupe adjudication.** Pairs scoring in `[0.65, 0.80)` are too ambiguous for a fixed threshold, so the model
  sees both records and decides same-person or not — capped at 20 pairs per run, each decision written to
  `dedupe_report.json.gray_zone_adjudication`. In the committed slice the band caught exactly **one** pair, a
  within-batch one, and the model ruled `no_merge` with a written rationale; the live HubSpot-side search in
  `m1_hubspot_dedupe.json` returned `matches: 0, gray_zone_pairs: 0`, so no CRM-side pair was adjudicated there.
- **M1 firmographic inference.** Per-company industry (from `config/icp.yaml`'s vocabulary), employee-count estimate
  and a confidence, batched once per distinct company. Clay values overwrite the model's when they arrive; every
  field carries its own `industry_source` / `numemployees_source` (`clay|llm|rules|synthetic`).
- **M1 ICP scoring.** Tier + rationale + confidence per row. `icp_tier()` recomputes the same tier from the rules;
  a model tier more than one level away still ships, but sets `needs_review_reason=icp_disagreement` for a human.
  **That is the capability, not what the committed receipt shows.** In `out/receipts/m1-live-slice-30/`, all three
  ICP batches came back unusable against the free model's per-batch deadline, so `icp_rows_scored_by_llm: 0`,
  `icp_rows_rules_fallback: 32` and `source_mix.icp_source: {rules: 32}` — every tier in that slice is the rule
  table's, and the file says so rather than presenting a rules output as a model output.
- **M2 extraction → three segment variants.** One call is the only thing that reads the transcript (takeaways with
  `[MM:SS]` anchors, verbatim quotes, the moments worth a no-show's click, a personalisation matrix by CRM function
  and industry bucket); three further calls turn that into attendee / no-show / speaker copy with two subjects each.
- **M2 grounding verifier.** Every quote and timestamp in the extraction, the variants and the rendered bodies is
  re-matched against the transcript; a miss fails the run *before* a dispatch plan exists.
- **M3 extraction → per-asset generation → grounding gate.** One extraction pass, normalised against real transcript
  turns; every asset is generated off that verified extraction rather than guessing at the raw transcript. After
  generation `verify_grounding.py` re-checks every timestamp, quote and speaker attribution; a failing asset gets one
  corrective regeneration against its own failure, and if it still fails the run fails rather than ships. On the
  committed run that came to 46 checks, all passing: `youtube.md` 18, `infographic.md` 12, `social.md` 9 and
  `blog.md` 7. An earlier version of this receipt read 39/39 because the verifier matched speakers by surname only,
  so prose like "As Q explained" attributed nothing and every blog quote was skipped; after fixing that and adding
  elided-quote support, re-running the shipped verifier over the same committed assets gives 46/46. Reproduce it:
  `python3 modules/m3-repurpose/verify_grounding.py --transcript data/incoming/transcript.md --event data/incoming/event.json --assets-dir out/receipts/m3-live`
- **M4 anomalies, interest scores, movement narrative, committees** all come from the model, each with its evidence
  rows or rationale — and the deterministic block (Tukey Q3 + 1.5×IQR fence, weighted engagement score, funnel,
  7/14/30-day movement windows) computes the same quantities as a check.
- **Deterministic rules validate the model; they never silently replace it.** Every mismatch lands in
  `analysis.json.llm.validator.disagreements` and prints on the dashboard. Where a model call fails, the module says
  so: M1 degrades that batch to the rule table with a warning and a lane reason, M2 and M3 fail the run rather than
  ship stale copy. No module presents a rules output as a model output.

## Run it

Shortest honest path for a reviewer: replay first — zero keys, zero network.

```bash
python3 orchestrator/run_pipeline.py --offline --out out/replay
```

Or one module at a time:

```bash
python3 modules/m1-enrichment/enrich.py --offline --out out/replay/m1
```

```bash
python3 modules/m2-comms/comms.py --offline --out out/replay/m2
```

```bash
python3 modules/m3-repurpose/repurpose.py --offline --out out/replay/m3
```

```bash
python3 modules/m4-dashboard/dashboard.py all --offline --out out/replay/m4
```

```bash
open out/replay/m4/index.html
```

M2 and M3 refuse to replay unless their cached sample matches this event's transcript and `event.json` SHA-256, so a
replay can never be a different webinar's output. Python 3.9+, stdlib only; M3's clips additionally need `ffmpeg` and
`ffprobe` on `PATH` plus Pillow, and clips/images are live-only (never replayed).

The live lane is the default — drop `--offline`:

```bash
python3 orchestrator/run_pipeline.py --out out/live
```

Env var **names** (values live in `~/.config/postevent/*.env`, Railway variables or n8n variables; nothing is
printed by any module): `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `HUBSPOT_TOKEN`, `SARVAM_API_KEY`. The module API
adds `MODULE_API_TOKEN` and `PUBLIC_BASE_URL`; the n8n workflows read `MODULE_API_URL`, `MODULE_API_TOKEN`,
`CLAY_WEBHOOK_URL`, `WEBHOOK_URL`, `DRIVE_PARENT_FOLDER_ID` and require `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`.

n8n calls the modules over HTTP through the module API (`api/server.py`) — `POST /run`, one module or phase per call:

```bash
curl -s -X POST "$MODULE_API_URL/run" -H "Authorization: Bearer $MODULE_API_TOKEN" -H 'Content-Type: application/json' -d '{"module":"m1","live":true,"inputs":{"registrants_csv_path":"data/incoming/registrants.csv"}}'
```

Full request/response shape and the phase table per module: [`docs/module-api.md`](docs/module-api.md). Deployment
procedure: [`docs/deploy-module-api.md`](docs/deploy-module-api.md). Workflow node lists, credential names and
per-workflow caveats: [`orchestrator/n8n/railway/README.md`](orchestrator/n8n/railway/README.md).

## What is real and what is simulated

**Real.** The event, its public page and recording, and both speakers. The transcript (Sarvam batch STT, receipt
above). Every generated artifact — blog, chapters, infographic, social posts, email copy, dedupe adjudications, ICP
rationales, dashboard analysis — is the output of a real model call with a per-call receipt logging model, purpose,
latency, HTTP status and parse result. The HubSpot dedupe searches and the contact/company/association writes are
real API calls.

**Generated or simulated, and labelled as such everywhere:**

- **The registrant list is generated.** 150 rows from `data/incoming/tools/gen_registrants.py`. Employer companies
  and domains are real — so enrichment returns real firmographics — but every registrant's name and email address is
  invented. No real attendee list for this webinar exists that could be shared, so none was used.
- **M4's engagement stream is seeded.** Opens, clicks, page views, form fills and lifecycle changes did not happen on
  their own; `dashboard.py seed` writes them into HubSpot through the public API. Everything from it is labelled
  `source: "seeded"` in the seed receipt, on every snapshot engagement block, in each movement window's `source_mix`
  and on the dashboard's badge. The 30-day window holds **40** lifecycle transitions, dated `{"2026-09-13": 1,
  "2026-09-21": 39}`, splitting `{"hubspot_history": 33, "seeded": 7}`. The 39 on 2026-09-21 were all written by this
  pipeline that afternoon — the 7-day window isolates exactly those 39 and splits them
  `{"hubspot_history": 32, "seeded": 7}`: 32 by the M1 push, 7 by the seed phase. The 40th is older: the speaker
  contact `kai8karma+speaker-qhamirani@gmail.com` moving to `lead` at 2026-09-13T11:05:41, which predates both this
  run's push and its seed. The dashboard card and the narrative both disclose the seeding — the narrative in its
  second paragraph ("7 of the 39 transitions in the 7-day window were seeded by the pipeline"), not its first
  sentence, which is about movement plateauing.
- **The HubSpot portal is a developer test portal**, not a production Darwinbox portal.
- **Images: 0 of 3 shipped.** Every image attempt returned HTTP 402 — the OpenRouter key has never purchased credits.
  `out/receipts/m3-live/receipts/m3_images.json` and `manifest.json.notes` record the model, the status and the error;
  no placeholder image was written in their place.
- **The model lane runs mostly, but not entirely, on free-tier OpenRouter models.** M1, M3 and M4's committed runs
  used `nvidia/nemotron-3-super-120b-a12b:free` and its fallback chain, which is why some calls show provider
  502/429 retries. **M2's committed run did not**: all four calls in `out/receipts/m2-live/m2_llm_calls.json` ran on
  `google/gemini-2.5-flash-lite`, a paid model — roughly half a US cent for the four calls.
- **Speaker email addresses are not public**, so speaker mail is addressed to sender-controlled aliases
  (`event.json._provenance.speaker_emails`). The event page publishes no air date; `event.json.date` is an assumption,
  disclosed in `_provenance.date`.

## Status

**Verified**, each by the receipt linked in the map above. Counts are as recorded by the run or test that produced
them:

- M1 — live slice of 32 rows (30 registrants + the 2 speakers): 96.2% contact / 93.0% company verified
  completeness, PASS against the brief's 90% bar;
  live HubSpot dedupe search; 10 LLM calls attempted, 7 parsed cleanly. **ICP tiering in this slice was not model
  work** — two ICP batches timed out against the free model's 90-second per-batch deadline, which retired that model for the run, so the third was never attempted and all 32 tiers came from the rule table
  (`icp_rows_scored_by_llm: 0`); the model's verified contribution here is the one gray-zone dedupe adjudication
  and the firmographic inference (29 of 32 industries, 32 of 32 employee counts). **8 of the 32 rows (25.0%) still
  carry a generic fallback** — `jobtitle == "Attendee"` or `industry == "Other"` that no LLM pass and no Clay
  backfill resolved. That is `needs_review_broadened_pct: 25.0` in `quality_report.json`, and it is the honest
  figure; the narrow `needs_review_pct` on the same receipt reads `0.0` because it only counts the export column's
  two triggers. Completeness passing at 96.2% and a quarter of rows still needing a human are both true: a row with
  `jobtitle == "Attendee"` is populated, just not informative. Push of 32 contacts / 23 companies
  / 27 associations into the test portal is **what the upsert calls reported** — that receipt's `verify` block reads
  `not_run`, so no independent read-back is committed with it.
- M2 — grounding 81/81, 0 failed; 4 live calls, all HTTP 200.
- M3 — blog 1106 words inside the 800–1200 gate, 18 chapters, 8 social posts across both platforms; grounding 46/46
  over `youtube.md` (18), `infographic.md` (12), `social.md` (9) and `blog.md` (7); 3 clips cut in both ratios — the
  SRT captions and thumbnails are in the package, the six MP4s are not and are regenerated by `make_clips.py` after
  re-downloading the chapter media, which is gitignored and does not ship.
- M4 — all four phases run live against the HubSpot test portal: seed 16 contacts / 73 engagement events / 12
  lifecycle updates through the contact-property fallback (`m4_seed.json.lifecycle_updates` is 12 — the number of
  lifecycle writes the seed phase issued; the movement windows attribute 7 transitions to the seed
  (`source_mix.seeded: 7`, the same 7 in the 7-, 14- and 30-day windows). The two count different things — writes
  issued vs. stage changes HubSpot's property history then recorded — and the receipts do not reconcile the gap
  further), sync 32 contacts + 23 companies over 6 HubSpot calls all
  HTTP 200, analyze 4/4 completions with 4 validator agreements and 3 disagreements recorded. Plus 37 zero-network
  tests (`modules/m4-dashboard/test_dashboard.py`) and 76 assertions over the module API's M4 surface
  (`api/test_server_m4.py`).

**Not yet — open, not done:**

- The four n8n workflows are authored and audited green (0 failures; warnings and every unverified node parameter are
  listed in `orchestrator/n8n/railway/README.md`), but **none has been imported into a running n8n instance**.
- **No Railway module-API URL is live.** `docs/deploy-module-api.md` is the procedure, not a record of a deployment.
  The container itself is real and was exercised: `docker build` exits 0, `GET /health` returns 200 listing all four
  modules, `POST /run` completes an M1 `prepare` phase inside the container (150 rows in, 142 out) and
  `GET /artifacts/<run_id>/quality_report.json` serves the result — receipt
  [`out/receipts/module-api-container.json`](out/receipts/module-api-container.json). What is missing is hosting,
  not a working image.
- HubSpot **`marketing-email`, `transactional-email` and the custom-events API return 403 on this portal**. M2's send
  path therefore falls back to Gmail with HubSpot engagement logging; M4's `seed` falls back to `postevent_*` counter
  properties instead of custom behavioural events.
- **No email has been dispatched.** `approval_gate.json` reads `pending_human_approval` on every run so far.
- **No Drive upload has run** — no Google Drive OAuth credential is connected, and `manifest.json.shared_drive` is
  empty.
- **Clay has never run.** The brief names Clay as the M1 firmographic source; the integration exists as the
  `--clay-results` merge path (with `--emit-clay-domains`) and a leg in the n8n M1 workflow, but no Clay run has
  happened. `out/receipts/m1-live-slice-30/quality_report.json` records `clay_domains_supplied: 0`,
  `clay_domains_applied: 0` and an empty `run_urls`, and no row in `outputs/enriched.json` carries a `clay` source —
  every firmographic in the committed slice came from the model or the rule tables (`industry_source`: 29 `llm`,
  3 `rules`; `numemployees_source`: 32 `llm`).
- **OpenRouter credits are at zero**, so the live lane runs free models and image generation stays at 402.
