# M1 Lead Enrichment — Railway n8n workflow

`m1-lead-enrichment.json` — importable n8n workflow (target: self-hosted n8n v1.x/2.x on
Railway). n8n owns the trigger, the Clay leg, the wait, and the evidence receipt; the module API
(`api/server.py`, see `docs/module-api.md`) owns parsing/dedupe/LLM/scoring/HubSpot push.

## Import

n8n → Workflows → Import from File → `m1-lead-enrichment.json`. Activate it, then set the env
vars below on the Railway n8n service.

## Env vars (n8n service, not the workflow)

| var | used for |
|---|---|
| `MODULE_API_URL` | base URL of the module API (`{MODULE_API_URL}/run`) |
| `MODULE_API_TOKEN` | `Authorization: Bearer` header on every module API call |
| `CLAY_WEBHOOK_URL` | the Clay table's webhook-source URL (one POST per domain) |
| `WEBHOOK_URL` | this n8n instance's public base URL, used to build the Clay callback URL |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` | **required** — self-hosted n8n blocks `$env` in expressions by default; every `$env.*` reference in this workflow (URLs, auth header, callback URL) resolves to nothing without this |

No n8n credentials are used anywhere in this file — the module API holds the HubSpot token
server-side; Clay auth is just an unguessable table-webhook URL.

## Trigger it

```bash
curl -X POST https://<your-n8n-host>/webhook/m1-run \
  -H "Content-Type: application/json" \
  -d '{"registrants_csv_url": "https://example.com/registrants.csv", "event_slug": "darwinbox-webinar", "skip_clay": false}'
```
Returns `{"run_id": "...", "status": "started"}` — the response only fires after the module API's
`prepare` phase returns, so you get the real `run_id`, not a bare ack.

## Why the Clay wait isn't a Wait-node webhook resume

v1's `master.json` waited on a **table-level** Clay "run complete" webhook — one POST carrying
every row, so a Wait node in `resume: webhook` mode (which resumes on the *first* POST and tears
the resume URL down) worked. This brief's Clay leg pushes **one row per domain** and expects
Clay's HTTP-API column to call back **once per row** — a Wait-node webhook resume would only ever
capture the first domain's result; every other callback would hit a dead URL.

Fix used here: `Wait for Clay Enrichment (10 min)` is a plain **time-based** wait
(`resume: timeInterval`), and a second, always-on webhook trigger (`Clay Result Callback
(webhook)`, path `/webhook/m1-clay-callback`) receives every per-domain callback and writes it
into `$getWorkflowStaticData('global')` keyed by `run_id`. `Assemble Clay Results` reads that
accumulator once the timer elapses. Static data is stored on the workflow and shared across all
its executions — the standard n8n pattern for collecting N async callbacks before continuing.

## Clay table setup (operator does this once, outside n8n)

1. **Source column**: webhook source, one row per POST — reads `domain`, `run_id`, `callback_url`
   from `Push Domain to Clay`'s body.
2. **Enrichment columns**: whatever finds industry / employee count / country for the domain
   (Clearbit/Clay's own enrichment, company lookup, etc).
3. **HTTP API column**: fires after the enrichment columns resolve, POSTs to the row's own
   `callback_url` value with body:
   ```json
   {"domain": "...", "industry": "...", "employee_count": 1234, "country": "IN", "run_url": "https://app.clay.com/..."}
   ```
   (`run_id` isn't in this body — the callback URL already targets `/webhook/m1-clay-callback`,
   and `Accumulate Clay Result` reads `run_id` off the POST body too, so also include `run_id`
   from the source row if your Clay table can reference it — simplest is to have the HTTP API
   column literally echo the row's own `domain` and `run_id` fields alongside the enrichment
   output.)

## Audit result

Audited with an n8n workflow linter that **is not part of this package**, so its verdict — 0
failures, 3 warnings — is not reproducible from this repo. Its warnings were:
- 2× orphan on the sticky notes (expected — sticky notes never have incoming connections)
- 1× `${...}` inside `Build Evidence Summary`'s `jsCode` — false positive, that's a real
  JS template literal (`` `${summary.run_id}.json` ``) inside a Code node, not an n8n expression
  field.

For the part a reviewer *can* re-run here, see [Reproducible static check](#reproducible-static-check).

**Flagged as unverified against a live instance** (per the skill's own guidance — don't claim
full verification you don't have):
- `Write Run Summary to Disk` uses `n8n-nodes-base.readWriteFile` (operation `write`). The brief
  asked for a "Write Binary File" node; that node is deprecated/removed in current n8n and folded
  into Read/Write Files from Disk. Parameter names (`fileName`, `dataPropertyName`) are a
  best-effort reconstruction — check them in your instance's node panel.
- `Skip Clay?`'s number condition uses operator `"equals"` — confirm this matches your n8n
  version's IF v2 number-operator naming.
- `Build Evidence Summary` assumes finalize's response has `summary.contacts_upserted`,
  `summary.verified_contact_pct`, `summary.verified_company_pct` — `docs/module-api.md` only
  shows a `prepare`-phase response example, not `finalize`'s, so this is inferred from the phase
  description, not confirmed against a real response.

## §M2 — Post-Event Comms (Railway)

`m2-post-event-comms.json` — n8n owns orchestration, the approval gate, the send (HubSpot or
Gmail), and the evidence receipt; the module API owns copy generation and the HubSpot engagement
log. Import the same way as M1 (Workflows → Import from File), then activate it.

### Nodes (28: 25 logic + 3 non-connecting: 2 sticky notes, the trigger)

| node | type | purpose |
|---|---|---|
| M2 Run (webhook) | webhook v2 | `POST /webhook/m2-run`, body `{m1_run_id?, event_slug, mode, approver_email}` |
| Call Module API - Generate | httpRequest v4.2 | `phase=generate`, timeout 900s |
| Generate OK? / Stop: Generate Failed | if v2 / stopAndError v1 | `ok:true` gate |
| Respond Awaiting Approval | respondToWebhook v1.1 | `{run_id, status, approve_url}` — `approve_url` = `$execution.resumeUrl`, resolvable before the Wait node has run |
| Build Approval Email Body | code v2 | counts + 3 subjects + resume-URL curl, from generate's response |
| Send Approval Request (Gmail) | gmail v2.1 | cred `Gmail account`, `continueOnFail:true` |
| Wait for Approval | wait v1.1 | resume: On Webhook Call, 24h limit |
| Call Module API - Approve | httpRequest v4.2 | `phase=approve`, body from the resume webhook's `$json.body` |
| Approve OK? / Stop: Approve Failed | if v2 / stopAndError v1 | `ok:true` gate |
| Split Recipients to Items | code v2 | `recipients[]` → one item each |
| Batch Recipients (1) | splitInBatches v3 | loop, 1 recipient/iteration |
| HubSpot Send Enabled? | if v2 | `$env.HUBSPOT_SEND_ENABLED == "true"` |
| HubSpot Single Send | httpRequest v4.2 | transactional single-send; `continueOnFail:true`, `neverError` so non-2xx is inspectable |
| HubSpot Send OK? | if v2 | 2xx → `Map HubSpot Result`; else → `Gmail Send (Recipient)` (fallback) |
| Map HubSpot Result / Map Gmail Result | code v2 | build the `dispatch_results.json` row; both loop back into `Batch Recipients (1)` |
| Gmail Send (Recipient) | gmail v2.1 | cred `Gmail account`, `continueOnFail:true`; fed by both the HubSpot-disabled path and the HubSpot-failure fallback |
| Aggregate Dispatch Results | code v2 | `$('Map HubSpot Result').all()` + `$('Map Gmail Result').all()` after the loop's `done` output |
| Call Module API - Log / Log OK? / Stop: Log Failed | httpRequest v4.2 / if v2 / stopAndError v1 | `phase=log` with `results[]` |
| Build Evidence Summary | code v2 | the within-24h receipt object |
| Write M2 Run Summary to Disk | readWriteFile v1 | `/home/node/.n8n/runs/<run_id>-m2.json` |
| M2 Run Summary (Final) | set v3.4 | re-emits the receipt as the execution's last node |

### Env vars (n8n service)

| var | used for |
|---|---|
| `MODULE_API_URL`, `MODULE_API_TOKEN` | module API calls (generate/approve/log) |
| `HUBSPOT_TOKEN` | Bearer auth on the HubSpot transactional single-send call only |
| `HUBSPOT_SEND_ENABLED` | `"true"` to take the HubSpot path; unset → Gmail |
| `HUBSPOT_TEMPLATE_ID` | HubSpot transactional `emailId` |
| `WEBHOOK_URL` | not read by this workflow directly (`approve_url` uses `$execution.resumeUrl`); listed for parity with M1 |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` | required, same as M1 |

### Trigger it

```bash
curl -X POST https://<your-n8n-host>/webhook/m2-run \
  -H "Content-Type: application/json" \
  -d '{"m1_run_id": "m1-20260913-1522", "event_slug": "darwinbox-ai-in-hr-2026-08-13", "mode": "demo", "approver_email": "kai8karma@gmail.com"}'
```
Returns `{"run_id": "...", "status": "awaiting_approval", "approve_url": "..."}` once `generate`
succeeds. The approval email (if the Gmail credential is set up) carries the same `approve_url`.

### Approve it (within 24h)

```bash
curl -X POST <approve_url> \
  -H "Content-Type: application/json" \
  -d '{"approved_by": "kai8karma@gmail.com", "mode": "demo"}'
```
This resumes the Wait node, calls `phase=approve`, and runs the send loop over the returned
`recipients[]`.

### Gmail OAuth2 credential setup (n8n UI)

Credentials → New → Gmail OAuth2 API → connect the sending Google account → **name it exactly
`Gmail account`** (both Gmail nodes reference this name, not an id). Used for the approval-request
mail and as the send/fallback path when HubSpot sending is off or fails.

### HubSpot single-send prerequisites

`HubSpot Single Send` needs **Marketing Hub Pro + the transactional email add-on** and a portal
with the `transactional-email` scope on its access token. If your portal doesn't have that: leave
`HUBSPOT_SEND_ENABLED` unset — every recipient goes out via Gmail instead, and engagements are
still logged to HubSpot by the module API's `log` phase regardless of which provider sent the
mail (same as M1, no n8n-side HubSpot credential object is used for sending; only a raw Bearer
token from `$env.HUBSPOT_TOKEN`).

### "Within 24 hours" evidence

`Build Evidence Summary` writes `hours_after_close` = the first successful send's timestamp minus
`event_close_ts` (read from generate's plan) — a negative-if-early, small-positive-if-fast number
that IS the SLA proof, written to
`/home/node/.n8n/runs/<run_id>-m2.json` and returned as the execution's final output. `providers`
lists which of `hubspot`/`gmail` actually carried traffic; `failed` counts rows that never
succeeded but were still logged, never dropped, per the brief.

**Flagged as unverified against a live instance**: `Build Approval Email Body` and `Build Evidence
Summary` both assume the `generate` phase's response `summary` mirrors `dispatch_plan.json`'s
`counts`/`variants`/`event_close_ts` fields — `docs/module-api.md` shows the plan file's own shape
but not the API envelope around it for this phase, so these paths are inferred, not confirmed.

## §M3 — Content Repurposing (Railway)

`m3-content-repurposing.json` — n8n owns orchestration, the Drive upload leg, and the evidence
receipt; the module API owns transcription, extraction/blog/image/clip generation, and the
manifest write. Import the same way as M1/M2 (Workflows → Import from File), then activate it.

### Nodes (26: 24 logic + 2 sticky notes)

| node | type | purpose |
|---|---|---|
| M3 Run (webhook) | webhook v2 | `POST /webhook/m3-run`, body `{event_slug, transcribe?, images?, clips?}` |
| Transcribe Requested? | if v2 | routes on `body.transcribe` |
| Call Module API - Transcribe | httpRequest v4.2 | `phase=transcribe`, timeout 1800s |
| Transcribe OK? / Stop: Transcribe Failed | if v2 / stopAndError v1 | `ok:true` gate |
| Build Run Body (Transcribed) / (No Transcribe) | code v2 | both emit `{event_slug, run_id, images, clips}` so `Call Module API - Run` sees one shape regardless of branch; the transcribed branch carries transcribe's `run_id` forward so `run` continues the same run |
| Call Module API - Run | httpRequest v4.2 | `phase=run`, `inputs.options.{images,clips}`, timeout 1800s |
| Run OK? / Stop: Run Failed | if v2 / stopAndError v1 | `ok:true` gate |
| Respond to Webhook | respondToWebhook v1.1 | `{run_id, status:"generated", summary}` — fires before the Drive leg, execution continues in the background |
| Create Drive Folder | googleDrive v3 | `resource:"folder"`, `operation:"create"`, cred `Google Drive account` |
| Split Next Files | code v2 | `next.files[]` → one item each |
| Batch Files (1) | splitInBatches v3 | loop, 1 file/iteration |
| Download File | httpRequest v4.2 | `{{$env.MODULE_API_URL}}{{$json.url}}`, bearer header, `responseFormat: file` |
| Upload File to Drive | googleDrive v3 | `resource:"file"`, `operation:"upload"`, cred `Google Drive account` |
| Map Drive Upload Result | code v2 | `{name, drive_file_id, web_view_link}`, loops back into `Batch Files (1)` |
| Aggregate Drive Uploads | code v2 | `$('Map Drive Upload Result').all()` after the loop's `done` output |
| Call Module API - Record / Record OK? / Stop: Record Failed | httpRequest v4.2 / if v2 / stopAndError v1 | `phase=record` with `drive:{folder_url,folder_id,files}` |
| Build Evidence Summary | code v2 | the receipt object |
| Write M3 Run Summary to Disk | readWriteFile v1 | `/home/node/.n8n/runs/<run_id>-m3.json` |
| M3 Run Summary (Final) | set v3.4 | re-emits the receipt as the execution's last node |

### Env vars (n8n service)

| var | used for |
|---|---|
| `MODULE_API_URL`, `MODULE_API_TOKEN` | module API calls (transcribe/run/record), same pattern as m1/m2 |
| `DRIVE_PARENT_FOLDER_ID` | Drive folder id the run's evidence folder is created under; unset → Drive root (`root`) |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` | required, same as m1/m2 |

### Google Drive OAuth2 credential setup (n8n UI)

Credentials → New → Google Drive OAuth2 API → connect the target Google account (scope
`https://www.googleapis.com/auth/drive.file` is enough — this workflow only creates folders and
uploads files it just created, never browses/reads the rest of the Drive) → **name it exactly
`Google Drive account`**. Both Google Drive nodes (`Create Drive Folder`, `Upload File to Drive`)
reference this name, not an id.

### Trigger it

```bash
curl -X POST https://<your-n8n-host>/webhook/m3-run \
  -H "Content-Type: application/json" \
  -d '{"event_slug": "darwinbox-ai-in-hr-2026-08-13", "transcribe": true, "images": true, "clips": true}'
```
Returns `{"run_id": "...", "status": "generated", "summary": {...}}` once `phase=run` succeeds —
the Drive upload and `phase=record` continue in the background after the response is sent.

### What the Drive folder looks like when done

One folder per run, directly under `$env.DRIVE_PARENT_FOLDER_ID` (or Drive root), named
`Post-Event/<event_slug>/<run_id>` — e.g. `Post-Event/darwinbox-ai-in-hr-2026-08-13/m3-20260916-1104`.
Drive allows `/` as a plain character in a folder name, so this is one folder with a path-shaped
label tagging it to its event and run, not three nested folders. Inside it: every file `next.files`
listed for the run — `blog.md`, `youtube.md`, `infographic.md`, `social.md`, `extraction.json`,
`visuals/*.png`, `clips/*.mp4` + `*.srt`, `manifest.json` — uploaded with their original names.
`manifest.json.shared_drive` (written by `phase=record`) then carries the folder's `folder_url` and
each file's `drive_file_id`/`web_view_link`, so the run's manifest and the shared Drive folder
point at each other — the "saved to a shared drive and tagged by event" proof.

### Audit result

Same off-package linter as m1: 0 failures, 2 warnings, both expected — orphan warnings on the 2
sticky notes, which never have incoming connections. That verdict is not reproducible from this
repo. What is: `json.load` parses and every `$('Node')` reference resolves to a node that exists —
26 nodes, 0 unresolved references, 0 duplicate names, via
[Reproducible static check](#reproducible-static-check).

**Flagged as unverified against a live instance** (per the skill's own audit rule):
- Both `Create Drive Folder` and `Upload File to Drive` use `n8n-nodes-base.googleDrive` v3 with a
  best-effort reconstruction of the `driveId`/`folderId` resourceLocator shape and
  `inputDataFieldName` — the brief flagged this as an acceptable risk upfront ("if unsure of exact
  parameter names, say so"); confirm field names in your instance's node panel before relying on
  this in production, same caveat m1/m2 carry for their `readWriteFile` nodes.
- `Call Module API - Transcribe`'s body adds `inputs.event_slug` beyond the brief's literal
  shorthand (`{module,phase,live}` only) — without it the module has no way to know which event's
  recording to transcribe; inferred from the pattern every other M1/M2/M3 phase call uses, not
  confirmed against a live `transcribe` response.
- `Build Run Body (No Transcribe)` assumes the module API can locate an existing transcript by
  `event_slug` alone when no `run_id` is supplied (the skip-transcribe use case) — `docs/module-api.md`
  doesn't document this resolution path.
- `webViewLink` on both Drive nodes' responses is assumed to be the Drive API's own field name,
  carried through unchanged by the n8n node — not confirmed against a live call.

## §M4 — Lead Intelligence (Railway)

`m4-lead-intelligence.json` — n8n owns the trigger and phase sequencing (seed → sync → analyze →
render); the module API owns every HubSpot read/write, the LLM analysis, and the dashboard render.
Re-runs sync → analyze → render against the live HubSpot portal on a schedule to keep the
dashboard/narrative fresh, and (once, by hand) seeds the event's simulated engagement stream first.

**Trigger cadence**: Schedule Trigger every 6h, OR Manual Trigger for the seed run / backfills.
Both feed `Build Run Params`, which builds `run_id` (`m4-<yyyyMMdd-HHmm>`), `event_slug` (default
`darwinbox-ai-in-hr-2026-08-13`, overridable by pinning data on `Manual Trigger` — no webhook body
here), and `seed` (default `false`). **Seed-once rule**: run `phase=seed` once, by hand, after the
M1 push (pin `{"seed": true}`, execute); every later run should leave `seed=false` — re-seeding
every 6h would duplicate the simulated engagement history.

**Env vars (n8n service)**: `MODULE_API_URL`, `MODULE_API_TOKEN` (module API calls, all 4 phases),
`EVIDENCE_EMAIL` (recipient for the optional, disabled evidence email).

**Import (REST)**:
```bash
curl -X POST https://<your-n8n-host>/api/v1/workflows \
  -H "X-N8N-API-KEY: <your n8n API key>" -H "Content-Type: application/json" \
  -d @m4-lead-intelligence.json
```
Then set the env vars above; for the evidence mail, connect a Gmail OAuth2 credential named exactly
`Gmail account` and enable `Send Evidence (Gmail)`.

**Evidence item** (`Build Evidence Summary`'s output, no disk write unlike M1–M3): `{run_id,
event_slug, sync: {contacts, companies, engagements, events, lifecycle_changes, method}, analyze:
{anomalies, scored, committees, model, lane}, render: {mql_rate, top_accounts, narrative_source},
dashboard_url, narrative_refresh_url, finished_at}`.

**Audit result**: same off-package linter — 0 failures, 2 warnings (orphan on the sticky note,
expected; `${...}` false positive inside `Assert * OK`'s `jsCode`, same as m1/m2/m3); not
reproducible from this repo. Reproducible here: 15 nodes, 0 unresolved `$('Node')` refs, 0
duplicates — see [Reproducible static check](#reproducible-static-check).

**Unverified until import**: `$env.*` readable inside a Code node like an expression field; the
`Assert * OK` throw halting the execution like `stopAndError` does elsewhere; pinning data on
`Manual Trigger` as the override path (no webhook exists otherwise); `scheduleTrigger`'s
`rule.interval` shape — none confirmed against a live instance.

## Reproducible static check

No validator ships with this package. This is the whole of what can be checked from the repo, with
no n8n instance and no network — JSON parses, no duplicate node names, and every `$('Node')`
reference resolves to a node that exists:

```bash
python3 - <<'EOF'
import json, re, pathlib
for f in sorted(pathlib.Path("orchestrator/n8n/railway").glob("*.json")):
    t = f.read_text(); wf = json.loads(t)
    names = [n["name"] for n in wf["nodes"]]
    refs = set(re.findall(r"\$\('([^']+)'\)", t)) - {"Node", "NodeName"}
    print(f.name, "nodes", len(names), "dupes", len(names) - len(set(names)), "unresolved", sorted(refs - set(names)))
EOF
```

Expected output:

```
m1-lead-enrichment.json nodes 21 dupes 0 unresolved []
m2-post-event-comms.json nodes 28 dupes 0 unresolved []
m3-content-repurposing.json nodes 26 dupes 0 unresolved []
m4-lead-intelligence.json nodes 15 dupes 0 unresolved []
```

`Node` and `NodeName` are excluded because they appear only as literal placeholders in node `notes`
prose (e.g. "`$('NodeName').all()` collects every run of that named node"), never as expressions.

This check says nothing about whether the workflows *run*. None of the four has been imported into
a running n8n instance — every "Unverified until import" note above still stands.
