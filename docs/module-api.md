# Module API — contract (v2)

One small HTTP service (`api/server.py`, stdlib only) hosts the four modules so n8n can call them
as real pipeline stages. Deployed on Railway next to n8n (no serverless timeout). Vercel keeps
the console + dashboard only.

## Endpoints

| method | path | purpose |
|---|---|---|
| GET | `/health` | `{ok, modules, live_available, versions}` |
| POST | `/run` | run one module (or phase) — body below |
| GET | `/artifacts/<run_id>/<path>` | fetch any file the run wrote |

Auth: `Authorization: Bearer $MODULE_API_TOKEN` on `/run` and `/artifacts` (n8n holds the token).

## POST /run body

```json
{"module": "m1", "phase": "prepare", "live": true, "run_id": "<optional, reuse to continue a run>",
 "inputs": {"registrants_csv_url": "https://…", "registrants_csv_path": "data/incoming/registrants.csv",
            "clay_results": {"<domain>": {"industry": "…", "employee_count": 1234, "country": "IN",
                                          "source": "clay", "run_url": "https://app.clay.com/…"}}}}
```

Response (always JSON, always HTTP 200 unless the service itself broke):

```json
{"ok": true, "module": "m1", "phase": "prepare", "lane": "live", "run_id": "m1-20260913-1522",
 "seconds": 84.2, "model": "nvidia/nemotron-3-super-120b-a12b:free",
 "summary": {"input_rows": 150, "output_rows": 142, "hubspot_matches": 25, "llm_calls": 9},
 "artifacts": {"hubspot_ready.csv": "/artifacts/m1-…/hubspot_ready.csv", "quality_report.json": "…"},
 "receipts": ["/artifacts/m1-…/receipts/m1_inference.json"],
 "next": {"clay_domains": ["infosys.com", "…"]}, "notes": []}
```

`ok:false` carries `error` and the partial log. Nothing is ever faked: if a backend is missing the
response says `lane: "offline"` and lists what was skipped in `notes`.

## Module phases

| module | phases | what runs |
|---|---|---|
| m1 | `prepare` | load registrants → dedupe within batch → dedupe against HubSpot (Search API) → LLM adjudicates gray-zone pairs → LLM infers title/function/seniority/industry/size with confidence → LLM ICP score + rationale (rules validate) → writes interim rows; returns `next.clay_domains` (domains still missing/low-confidence firmographics) |
| m1 | `finalize` | applies `inputs.clay_results` (Clay wins over LLM, LLM over rules; per-field `*_source` provenance) → lifecycle + owner routing → completeness (verified metric) → `hubspot_ready.csv` + companies/contacts CSVs → **push to HubSpot** (properties, companies, contacts, associations, verify) → receipts |
| m1 | *(omitted)* | prepare + finalize in one call, no Clay leg (n8n skips Clay when the table isn't configured) |
| m2 | `generate`, `dispatch` | W2 |
| m3 | `run` | W3 |
| m4 | `sync`, `render` | W4 |

## Receipts (every live call writes one)

`out/<run_id>/receipts/*.json` — provider, model, request count, tokens, latency, HTTP statuses,
input/output ids. The dashboard and README link to these; nothing in prose without a file behind it.

## Env

`OPENROUTER_API_KEY`, `OPENROUTER_MODEL` (comma chain), `HUBSPOT_TOKEN`, `SARVAM_API_KEY`,
`MODULE_API_TOKEN`, `PUBLIC_BASE_URL`. Read from env only (Railway variables); locally via
`~/.config/postevent/*.env`.

## M2 — phases and files (W2)

| phase | what runs | writes |
|---|---|---|
| `generate` | comms.py (live default): LLM extracts takeaways + quotes from the real transcript, writes 3 variants × 2 subjects, renders one personalised message per recipient (function/industry merge), speaker mail carries the performance snapshot; approval gate `pending_human_approval` | `dispatch_plan.json`, `emails/*.md`, `approval_gate.json`, `receipts/m2_llm_calls.json` |
| `approve` | flips `approval_gate.json` (`approved_by`, `approved_at`, `mode: demo|full`) and returns the recipient list n8n should send: `demo` = recipients with `demo_redirect_to` + speakers; `full` = everyone mailable | `approval_gate.json` |
| `log` | takes n8n's `dispatch_results.json`, creates one HubSpot **email engagement** per sent message (CRM v3 `emails` object, associated to the contact, `hs_email_status=SENT`, subject/body/UTM links, external message id) — only for rows with a real `message_id`; never fabricates | `receipts/m2_hubspot_log.json` |

`dispatch_plan.json`:
```json
{"run_id": "m2-…", "event_slug": "darwinbox-ai-in-hr-2026-08-13", "generated_at": "…", "lane": "live",
 "event_close_ts": "2026-08-13T10:33:00-04:00",
 "variants": {"attendee": {"subject_a": "…", "subject_b": "…", "body_md": "…", "takeaways": ["…"]},
              "no_show": {"…": "…"}, "speaker": {"…": "…", "snapshot": {"registrants": 150, "attendees": 75, "attendance_rate": 0.5,
                                                   "avg_minutes": 31.2, "top_accounts": ["Infosys", "…"]}}},
 "recipients": [{"email": "…", "hubspot_contact_id": "123|null", "segment": "attendee|no_show|speaker",
                 "firstname": "…", "function": "…", "industry_bucket": "…", "subject": "…",
                 "body_html": "…", "body_text": "…",
                 "links": {"recording": "https://…?utm_source=…", "cta": "https://…?utm_…"},
                 "utm": {"source": "…", "medium": "email", "campaign": "…", "content": "…"},
                 "demo_redirect_to": "kai8karma+attendee@gmail.com|null"}],
 "approval": {"status": "pending_human_approval", "approved_by": null, "approved_at": null, "mode": null},
 "counts": {"attendee": 0, "no_show": 0, "speaker": 0, "mailable": 0, "suppressed": 0}}
```

`dispatch_results.json` (n8n → `log`):
```json
[{"email": "…", "segment": "attendee", "provider": "gmail|hubspot", "message_id": "…", "sent_to": "kai8karma+attendee@gmail.com",
  "sent_at": "…", "status": "sent|failed", "error": null}]
```
Demo dispatch = the recipients whose `demo_redirect_to` is set (one attendee, one no-show, redirected to Kai-controlled aliases because registrant people are synthetic) plus the two speakers (already alias addresses). HubSpot engagements are logged on the real synthetic contact with `sent_to` recorded, so the CRM shows the send on the right record.

`api/server.py`'s `/run` response `summary` for each M2 phase (read straight from the files above, never recomputed — a missing file lands in `notes` instead of failing the field): `generate` → `{recipients, counts, approval_status, event_close_ts, event_slug, subjects}` where `counts`/`event_close_ts`/`event_slug` are copied verbatim from `dispatch_plan.json` and `subjects` is `{attendee|no_show|speaker: [subject_a, subject_b]}` built from its `variants` block; `approve` → top-level `recipients` (the filtered list per the `demo`/`full` rule above) plus `summary.counts = {selected, mode}`; `log` → `summary = {logged, skipped, errors}` plus the full receipt under `receipt` (same shape `log_dispatch.py` writes to `receipts/m2_hubspot_log.json`).

## M3 — phases and files (W3)

| phase | what runs | writes |
|---|---|---|
| `transcribe` | `transcribe_batch.py` (Sarvam saaras:v3 batch, diarization + timestamps) on `event.json.recording_files` (downloaded from the public CDN if not local) → `build_transcript.py` → `transcript.md` + `.vtt` | `transcript.md`, `receipts/transcription.json`, `receipts/transcription/*.sarvam.json` |
| `run` | `repurpose.py` (live default): one LLM extraction over the transcript → blog (800–1200, hard gate with one retry), YouTube chapters + description + thumbnail brief, infographic outline with data points, 5–10 social posts (LinkedIn + X, distinct hooks), grounding verifier over every quote/timestamp; **image generation** (thumbnail + 2 social visuals) via OpenRouter image-capable model; **clips**: top 3 moments from the extraction cut with ffmpeg from the recording, 30–60 s, 16:9 and 9:16 (centre crop), captions SRT built from the Sarvam word timestamps and burned into the 9:16 | `extraction.json`, `blog.md`, `youtube.md`, `infographic.md`, `social.md`, `visuals/*.png`, `clips/*.mp4` + `*.srt`, `grounding_report.json`, `manifest.json`, `receipts/m3_llm_calls.json`, `receipts/m3_images.json`, `receipts/m3_clips.json` |
| `record` | takes the Drive upload results from n8n and writes them into the manifest | `drive_manifest.json`, `manifest.json.shared_drive` |

**Multiple events:** `transcribe` and `run` both accept `inputs.event_slug`. Omitted, or the literal
`darwinbox-ai-in-hr-2026-08-13`, resolves to the bundled fixture (`data/incoming/event.json` /
`data/incoming/transcript.md`); any other slug resolves to `data/events/<slug>/event.json` /
`data/events/<slug>/transcript.md` and fails loud (`ok:false`, no fixture fallback) if that `transcript.md`
is missing. `run` additionally checks its own `run_id` before falling back to the slug: with no
`inputs.transcript_md_path` given, if `out/api/<run_id>/transcript.md` already exists (written by a prior
`transcribe` call reusing that same `run_id`), `run` uses that freshly produced transcript instead of the
slug default — so a `transcribe` → `run` pair sharing one `run_id` always regenerates from what was just
transcribed. Explicit `inputs.transcript_md_path` / `inputs.event_json_path` win over both the `run_id` reuse
and the slug resolution.

`run` response: `summary` = `{blog_words, chapters, posts: {linkedin, x}, images, clips, grounding: {checked, passed}, lane, models}`,
`artifacts` map as usual, and `next.files` = `[{name, path, url, mime, kind: "blog|youtube|infographic|social|extraction|visual|clip|caption|manifest"}]`
for n8n's Google Drive node. `record` body: `{run_id, drive: {folder_url, folder_id, files: [{name, drive_file_id, web_view_link}]}}`.

`manifest.json`: `{event_slug, generated_at, lane, models: {text, image}, files: [{name, kind, bytes, source: "llm|image_model|ffmpeg|sarvam", grounded: true|false|null}], shared_drive: {folder_url, files: [...]}}` — every file names its source; nothing pretends a CSS render is a generated image.

Fuel rules: text calls run on the free nemotron model with small prompts when no credits exist; image generation needs a paid model and is skipped with a `notes` entry (never a placeholder PNG) when the key has no credits; clips need only ffmpeg.

## M4 — phases and files (W4)

Source of truth is the HubSpot portal, not a fixture: M1 pushed the contacts (tagged `postevent_event`), M2 logged the
email engagements, and M4 writes the engagement stream and lifecycle changes INTO HubSpot before reading anything back.

| phase | what runs | writes |
|---|---|---|
| `seed` | writes the event's engagement stream into HubSpot for the tagged contacts: tries custom behavioural events (`POST /events/v3/event-definitions` once, then `POST /events/v3/send` per open/click/pageview/form_fill), falls back to contact properties (`postevent_opens/clicks/pageviews/form_fills`, `postevent_last_engaged`) when the portal refuses event definitions; lifecycle stage changes are applied as real `lifecyclestage` updates so HubSpot's own property history holds the movement. Registrant people are synthetic, so this stream is simulated and labelled `source: "seeded"` everywhere it appears; opens/clicks on the demo dispatch are real only when sent through HubSpot | `receipts/m4_seed.json` (method used, counts, errors) |
| `sync` | pulls from HubSpot: tagged contacts with `propertiesWithHistory=lifecyclestage`, companies + associations, email engagements, custom events or the counter properties | `snapshot.json`, `receipts/m4_hubspot_sync.json` (endpoints, pages, counts) |
| `analyze` | LLM over the snapshot: engagement anomalies (with the evidence rows), lead-interest score per contact with rationale, **narrated stage movement** across 7/14/30 days, top accounts and buying committees (≥2 engaged contacts at one company). Deterministic math computes the same metrics as a validator; disagreements are flagged, never hidden | `analysis.json`, `receipts/m4_llm_calls.json` |
| `render` | self-contained `index.html`: attendee→MQL conversion, top engaged accounts + contacts, lifecycle movement 7/14/30d, anomaly panel, buying-committee map, narrative block with a source badge; `GET /narrative/<run_id>?refresh=1` re-runs `analyze` so the narrative refreshes on load when the page is served from the module API (Vercel's `/api/narrative` proxies to it) | `index.html`, `dashboard_data.json` |

`sync` response `summary` = `{contacts, companies, engagements, events, lifecycle_changes, method}`; `analyze` `summary` = `{anomalies, scored, committees, model, lane}`; `render` `summary` = `{mql_rate, top_accounts, narrative_source}`.
