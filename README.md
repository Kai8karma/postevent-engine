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

## Brief → artifact map

| brief asks | what runs | where the output is | receipt |
|---|---|---|---|
| **M1 Lead List Enrichment** — dedupe/fuzzy-match against HubSpot, infer title/function/seniority/size/industry, enrich company data via Clay, score ICP fit; HubSpot-ready file >90% complete with tier, region, owner, lifecycle | [`modules/m1-enrichment/enrich.py`](modules/m1-enrichment/enrich.py) (live default): HubSpot Search dedupe → LLM gray-zone adjudication → LLM row inference + firmographics + ICP tier with rationale (rules validate) → `--clay-results` merge with per-field `*_source` → [`push_to_hubspot.py`](modules/m1-enrichment/push_to_hubspot.py) upsert. n8n: [`m1-lead-enrichment.json`](orchestrator/n8n/railway/m1-lead-enrichment.json) | `<out>/hubspot_ready.csv`, `hubspot_contacts.csv`, `hubspot_companies.csv`, `quality_report.json`, `dedupe_report.json`, `live_inference_report.json` | [`out/receipts/m1-live-slice-30/`](out/receipts/m1-live-slice-30/) — 30-row live slice: **96.2% contact / 93.0% company verified completeness, PASS** at the 90% bar, 10 LLM calls, live HubSpot search. Push: [`out/receipts/m1-push-slice30-2026-09-16.json`](out/receipts/m1-push-slice30-2026-09-16.json) — 32 contacts, 23 companies, 27 associations upserted into the test portal |
| **M2 Post-Event Communications** — segment-specific copy, subject-line variants, takeaway snippets personalised by role/industry; n8n orchestration; HubSpot send + tracking; UTM on every link; three variants inside 24h of close | [`modules/m2-comms/comms.py`](modules/m2-comms/comms.py): one extraction call over the real transcript + one call per segment (attendee / no-show / speaker), per-recipient render off M1's `function`/`industry`, grounding gate, approval gate. Module API phases `generate`/`approve`/`log` ([`docs/module-api.md`](docs/module-api.md) §M2). n8n: [`m2-post-event-comms.json`](orchestrator/n8n/railway/m2-post-event-comms.json) | `<out>/dispatch_plan.json`, `emails/<segment>.md`, `approval_gate.json`, `extraction.json`, `grounding.json` | [`out/receipts/m2-live/`](out/receipts/m2-live/) — `grounding.json` **81/81 checks passed, 0 failed**; `m2_llm_calls.json` 4 live calls, all HTTP 200 |
| **M3 Content Repurposing** — transcription API; extract moments/insights/quotes/data points; blog 800–1200 words, YouTube chapters + description + thumbnail brief, infographic outline, 5–10 social posts; image generation; saved to a shared drive, tagged by event | [`transcribe_batch.py`](modules/m3-repurpose/transcribe_batch.py) (Sarvam batch STT) → [`repurpose.py`](modules/m3-repurpose/repurpose.py): one extraction pass, every asset generated off it, [`verify_grounding.py`](modules/m3-repurpose/verify_grounding.py) hard gate with a repair pass → `make_clips.py` (ffmpeg, 16:9 + 9:16 + burned captions) → `gen_visuals.py` (image model). n8n: [`m3-content-repurposing.json`](orchestrator/n8n/railway/m3-content-repurposing.json) + the API's `record` phase for the Drive leg | `<out>/blog.md`, `youtube.md`, `infographic.md`, `social.md`, `extraction.json`, `grounding_report.json`, `manifest.json`, `clips/`, `visuals/` | [`out/receipts/m3-live/`](out/receipts/m3-live/) — blog **1106 words**, 18 chapters, **8 posts** (5 LinkedIn + 3 X), grounding **39/39**, **3 clips** × 2 ratios + SRT; `receipts/m3_images.json` records **0 of 3 images** (HTTP 402) |
| **M4 Lead Intelligence Dashboard** — HubSpot contacts, engagement events and lifecycle changes in; anomalies, lead-interest scores, narrated stage movement, top accounts and buying committees; HTML dashboard with attendee→MQL, 7/14/30-day movement, narrative refreshed on load | [`modules/m4-dashboard/dashboard.py`](modules/m4-dashboard/dashboard.py) `seed` → `sync` → `analyze` → `render`: writes the engagement stream and lifecycle changes into HubSpot, reads the portal back, LLM analysis checked by deterministic math, self-contained page; `GET /narrative/<run_id>?refresh=1` re-runs `analyze`. n8n: [`m4-lead-intelligence.json`](orchestrator/n8n/railway/m4-lead-intelligence.json) | `<out>/snapshot.json`, `analysis.json`, `dashboard_data.json`, `index.html` | [`out/receipts/m4-live-portal/`](out/receipts/m4-live-portal/) — all four phases live against the HubSpot test portal: `seed` fell back to contact properties after a 403 on custom event definitions (16 contacts matched, 73 engagement events, 12 lifecycle updates), `sync` read back 32 contacts + 23 companies over 6 calls all HTTP 200, `analyze` 4/4 completions with the validator logging 4 agreements and 3 disagreements, nothing overwritten. Earlier fixture-snapshot analyze: [`out/receipts/m4-live-analyze-fixture/`](out/receipts/m4-live-analyze-fixture/) |

## How the AI is the engine, not a wrapper

- **M1 dedupe adjudication.** Pairs scoring in `[0.65, 0.80)` are too ambiguous for a fixed threshold, so the model
  sees both records and decides same-person or not — capped at 20 pairs per run, each decision written to
  `dedupe_report.json.gray_zone_adjudication`.
- **M1 firmographic inference.** Per-company industry (from `config/icp.yaml`'s vocabulary), employee-count estimate
  and a confidence, batched once per distinct company. Clay values overwrite the model's when they arrive; every
  field carries its own `industry_source` / `numemployees_source` (`clay|llm|rules|synthetic`).
- **M1 ICP scoring.** Tier + rationale + confidence per row. `icp_tier()` recomputes the same tier from the rules;
  a model tier more than one level away still ships, but sets `needs_review_reason=icp_disagreement` for a human.
- **M2 extraction → three segment variants.** One call is the only thing that reads the transcript (takeaways with
  `[MM:SS]` anchors, verbatim quotes, the moments worth a no-show's click, a personalisation matrix by CRM function
  and industry bucket); three further calls turn that into attendee / no-show / speaker copy with two subjects each.
- **M2 grounding verifier.** Every quote and timestamp in the extraction, the variants and the rendered bodies is
  re-matched against the transcript; a miss fails the run *before* a dispatch plan exists.
- **M3 extraction → per-asset generation → grounding gate.** One extraction pass, normalised against real transcript
  turns; every asset is generated off that verified extraction rather than guessing at the raw transcript. After
  generation `verify_grounding.py` re-checks every timestamp, quote and speaker attribution; a failing asset gets one
  corrective regeneration against its own failure, and if it still fails the run fails rather than ships.
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
  and on the dashboard's badge. Every lifecycle transition in that portal was written by this pipeline — 32 by the M1
  push and 7 by the seed phase, all on one day — and the dashboard card and the narrative's first sentence say so.
- **The HubSpot portal is a developer test portal**, not a production Darwinbox portal.
- **Images: 0 of 3 shipped.** Every image attempt returned HTTP 402 — the OpenRouter key has never purchased credits.
  `out/receipts/m3-live/receipts/m3_images.json` and `manifest.json.notes` record the model, the status and the error;
  no placeholder image was written in their place.
- **The model lane runs on free-tier OpenRouter models today** (`nvidia/nemotron-3-super-120b-a12b:free` and its
  fallback chain), which is why the receipts name those models and why some calls show provider 502/429 retries.
- **Speaker email addresses are not public**, so speaker mail is addressed to sender-controlled aliases
  (`event.json._provenance.speaker_emails`). The event page publishes no air date; `event.json.date` is an assumption,
  disclosed in `_provenance.date`.

## Status

**Verified**, each by the receipt linked in the map above. Counts are as recorded by the run or test that produced
them — the full progress log is in [`_internal/REVISION-PLAN-2026-09-12.md`](_internal/REVISION-PLAN-2026-09-12.md):

- M1 — 30-row live slice: 96.2% contact / 93.0% company verified completeness, PASS against the brief's 90% bar;
  live HubSpot dedupe search; push of 32 contacts / 23 companies / 27 associations into the test portal.
- M2 — grounding 81/81, 0 failed; 4 live calls, all HTTP 200.
- M3 — blog 1106 words inside the 800–1200 gate, 18 chapters, 8 social posts across both platforms, grounding 39/39,
  3 clips in both ratios with SRT captions.
- M4 — all four phases run live against the HubSpot test portal: seed 16 contacts / 73 engagement events / 12
  lifecycle updates through the contact-property fallback, sync 32 contacts + 23 companies over 6 HubSpot calls all
  HTTP 200, analyze 4/4 completions with 4 validator agreements and 3 disagreements recorded. Plus 37 zero-network
  tests (`modules/m4-dashboard/test_dashboard.py`) and 76 assertions over the module API's M4 surface
  (`api/test_server_m4.py`).

**Not yet — open, not done:**

- The four n8n workflows are authored and audited green (0 failures; warnings and every unverified node parameter are
  listed in `orchestrator/n8n/railway/README.md`), but **none has been imported into a running n8n instance**.
- **No Railway module-API URL is live.** `docs/deploy-module-api.md` is the procedure, not a record of a deployment.
- HubSpot **`marketing-email`, `transactional-email` and the custom-events API return 403 on this portal**. M2's send
  path therefore falls back to Gmail with HubSpot engagement logging; M4's `seed` falls back to `postevent_*` counter
  properties instead of custom behavioural events.
- **No email has been dispatched.** `approval_gate.json` reads `pending_human_approval` on every run so far.
- **No Drive upload has run** — no Google Drive OAuth credential is connected, and `manifest.json.shared_drive` is
  empty.
- **OpenRouter credits are at zero**, so the live lane runs free models and image generation stays at 402.
