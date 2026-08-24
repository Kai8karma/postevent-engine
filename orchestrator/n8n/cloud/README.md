# orchestrator/n8n/cloud — two lanes

This directory ships two separate n8n workflows for the same post-event pipeline. They are independent files — import either one, or both, without conflict (different node names, no shared IDs). **Both are now live on the `agentkai.app.n8n.cloud` instance** (see "Current instance state" below) — this README documents what's actually running, not just what the files describe.

| file | workflow name | purpose | credentials needed |
|---|---|---|---|
| `demo-runner.json` | Post-Event Engine — Cloud Demo (no credentials) | Proves the pipeline runs, green, end to end, on a stock n8n Cloud account with nothing configured. | **none** |
| `master.json` | Post-Event Engine — Cloud Production | The real GTM lane: webinar platform → Clay enrichment → HubSpot contacts/companies/associations → approval gate → **Gmail send + HubSpot engagement-log dispatch** → Telegram notify (optional). | HubSpot App Token, HubSpot Header Auth, HubSpot Custom Auth (new), Gmail OAuth2 (new), Clay table webhook ID, webinar-platform API, Telegram bot token (see below) |

Both lanes are driven from the same source of truth for what the pipeline actually does: `orchestrator/run_pipeline.py` and the four module scripts under `modules/`.

## Current instance state (as of this update)

- **`master.json` → workflow id `rxJnu5ZATDFLJToD`**, name "Post-Event Engine — Cloud Production", **Active**. Its M2 dispatch section was just redesigned (see below) and the change was pushed to the live instance via the n8n API, then published.
- **`demo-runner.json` → workflow id `JrlK78Wy3hm1ai3U`**, name "Post-Event Engine — Cloud Demo (no credentials)", **Active**. This was a straight import (no logic changes) — it did not exist on the instance before this update.
- **No end-to-end execution of either workflow has happened on this instance.** Both were created/updated, validated, and published (activated) — publishing and validating are not executions. Only 3 trivial manual runs of the *old* production workflow exist in its execution history from before this update (1 success, 1 canceled, 1 error) — never a real end-to-end run through the approval gate and dispatch chain. Do not read "Active" as "proven to work."
- **Known gap, found while doing this update**: the live `rxJnu5ZATDFLJToD` workflow was discovered to be running an **older, simpler version** than this repo's `master.json` — it predates the idempotency guard (`Check Already Processed` / `Already Processed?`), the batching/loop nodes (`Split*ToItems`, `Batch*(100)`, `Merge Contact + Company Results`, `Build Association Pairs`), and the whole error-handling branch (`Shape Error Payload` / `Notify Team of Failure` / `Stop with Clear Error`). The M2 dispatch redesign below was therefore pushed to the live instance twice, in two different shapes:
  - **In the repo file** (`orchestrator/n8n/cloud/master.json`, the source of truth): the new dispatch nodes wire their error outputs to the existing `Shape Error Payload` fan-in, exactly like every other write node in that file.
  - **On the live instance** (which has no `Shape Error Payload` node yet): the same two new write nodes (`Send Demo Email (Gmail)`, `Log to HubSpot (email engagement)`) instead use `onError: stopWorkflow` — a failure there stops the execution loudly (visible as a red node in the Executions tab) rather than being silently swallowed by a nonexistent error sink. Each node's live-instance `notes` field says this explicitly and says to repoint it once the live workflow is reconciled with the repo file's fuller pipeline. **Reconciling the live instance to full parity with the repo file is separate, larger work, out of scope for this dispatch redesign.**

## Why two lanes

The previous state of this repo's n8n workflow was a single straight-line 13-node chain where everything past the first `Wait` node needed credentials nobody had wired — "not even a single complete run" in the executions list. `demo-runner.json` exists to answer that directly: it is a workflow a reviewer can execute right now, with zero setup, and watch complete. `master.json` is the real lane a production deployment would eventually run, once someone wires HubSpot/Clay/Gmail/Telegram.

## demo-runner.json — what it does

Instead of talking to HubSpot/Clay directly, every external call in this lane goes to the pipeline engine's own **public, keyless HTTP API**, deployed at:

```
POST https://postevent-engine.vercel.app/api/run
GET  https://postevent-engine.vercel.app/api/run
```

The engine actually runs the real `modules/m1-enrichment/enrich.py`, `m2-comms/comms.py`, `m3-repurpose/repurpose.py`, `m4-dashboard/build_dashboard.py` as subprocesses against a bundled fixture (150 registrants → 133 contacts / 30 companies, event `Pipeline After the Webinar: Turning Event Engagement into Revenue`, 2026-07-20) and returns their real output as JSON. No credentials are required to call it. See `api/run.py` and `api/vercel-api-notes.md` in the repo root for the exact contract.

### Canvas flow

1. **Manual Trigger** and **Webhook (postevent-demo)** (path `postevent-demo`, POST) both feed **Normalize Trigger Input** (Set node), which reconciles the two different input shapes (webhook body vs. manual-trigger empty object) into one: `event_name`, `live`, `registrants_csv`, `transcript_md`.
2. **Build Run Request** (Code) assembles the `/api/run` request body, defaulting `event_name` to the real event above when the trigger carried nothing. It deliberately *omits* `registrants_csv` / `transcript_md` when empty rather than sending `""` — `api/run.py`'s `materialize_event()` treats any non-`None` value (including an empty string) as a caller-supplied file and skips the bundled fixture, which would break the run.
3. **Derive Run Key** (Code) computes an idempotency key (`slug(event_name)__YYYY-MM-DD`) and carries it through to the response.
4. **Health Check — GET /api/run** confirms the engine is reachable and reports `live_available` (whether the deployment has an `OPENROUTER_API_KEY` for real LLM calls). **Service Healthy? (ok)** gates on `ok` only — deliberately not on `live_available`, since this demo lane always requests `live:false` by default and doesn't need an LLM key to complete. A transport failure or `ok:false` routes to **Engine API Unreachable** (`StopAndError`), so a down deployment fails loud instead of hanging.
5. **M1 — Enrichment → M2 — Comms → M3 — Repurpose → M4 — Dashboard**: four sequential `HTTP Request` POSTs to the same `/api/run` endpoint, each with `retryOnFail` (2-3 tries, 2s apart) and a 60s timeout (the API's own budget is ~55s). Each node's error output (`onError: continueErrorOutput`) feeds a shared **Shape Module Error** branch, which identifies the failed module and returns a legible `{ok:false, module, error, hint}` via **Respond: Module Error**.
6. After M1, **Completeness ≥ 90%? (SDR-handoff bar)** checks `summary.contact_completeness_pct` against the brief's own acceptance bar. Both branches reconverge on M2 — a below-bar run is flagged (`Flag: Below SDR-Handoff Bar`) but the demo still completes.
7. **Merge Module Summaries** (Code) collects all four module responses, and — since `api/run.py` always answers HTTP 200 even on a module-internal failure — also checks each module's own `ok` field to catch API-level failures that never triggered an HTTP error. **Respond: Success** returns the merged result as the webhook response; **Shape Run Summary (execution log)** is a terminal node giving a compact one-line view in the n8n Executions tab.

### Idempotency

`run_key` is deterministic per `event_name` + execution date. This demo lane performs no writes, so there's nothing to dedupe — the note on `Derive Run Key` and `Merge Module Summaries` explains how a production instance would check `run_key` against a store (Postgres row / n8n Data table / HubSpot custom object) before letting a write-side node fire twice for the same event.

## How to import (if you need a fresh copy)

Both workflows already exist on the `agentkai.app.n8n.cloud` instance (see "Current instance state" above), so you shouldn't need to import either from scratch. If you ever do (a second environment, a rollback, etc.):

1. In n8n: **Workflows → Import → Import from URL**.
2. Demo lane: `https://postevent-engine.vercel.app/n8n/demo-runner.json`
   Production lane: `https://postevent-engine.vercel.app/n8n/master.json`
3. **Gotcha hit while building this**: n8n's Import does **not** replace the nodes already on the canvas — it appends the imported workflow's nodes on top of whatever is there. If you import into a canvas that already has a workflow open, you end up with duplicated nodes. **Clear the canvas first** (open a new/blank workflow, or select-all + delete) before importing, or import into a brand-new workflow.

## How to fire the demo

**Via the UI**: open `demo-runner.json` (workflow id `JrlK78Wy3hm1ai3U`) in the n8n editor and click **Execute Workflow** (uses the Manual Trigger branch). The result appears in the execution's output pane on `Merge Module Summaries` / `Respond: Success`.

**Via curl**, against the workflow's *test* webhook URL (visible in the n8n editor when you click the Webhook node and hit "Listen for Test Event", or the *production* URL now that the workflow is Active):

```bash
curl -X POST "https://agentkai.app.n8n.cloud/webhook-test/postevent-demo" \
  -H "Content-Type: application/json" \
  -d '{
    "event_name": "Pipeline After the Webinar: Turning Event Engagement into Revenue",
    "live": false
  }'
```

Swap `webhook-test` for `webhook` to hit the production webhook URL instead (the workflow is Active, so the production URL is live). Omit `registrants_csv` / `transcript_md` entirely (as above) to run against the bundled fixture — that's what a reviewer should do for the default demo run.

**Reminder: this has not been done yet.** The workflow is imported, validated, and published — nobody has clicked Execute or curled it as part of this update.

### What a reviewer should expect to see

- The execution goes fully green (no red nodes) in well under a minute.
- The final JSON response (from `Respond: Success`) contains `ok: true`, a `run_key`, and a `modules` object with `m1`/`m2`/`m3`/`m4`, each carrying that module's real `summary` (e.g. `m1.summary.contact_completeness_pct`, `m4.summary.attendee_to_mql_pct`).
- If `contact_completeness_pct` happens to fall under 90 on a given run, the execution still completes — check the `Flag: Below SDR-Handoff Bar` node's output for the flag, and `modules.m1.summary` in the final response for the actual number.
- Killing network access to `postevent-engine.vercel.app` (or pointing the URL at something unreachable) should route the run into `Engine API Unreachable` and stop with a clear error message, not hang.

## master.json — production lane — M2 dispatch redesign

The dispatch step used to call HubSpot's Single-Send API directly (`HubSpot Single Send` node). That call can never succeed: HubSpot portal `247135551` is `accountType DEVELOPER_TEST`, which has no verified sending domain, so native HubSpot marketing/transactional send is impossible in this portal — not a wiring problem, a portal-tier limitation. That node has been replaced with an honest chain, between the `Approved?` gate and the (optional, disabled) `Notify Team` node:

1. **Stamp SLA (hours since event close)** (Code) — reads a new `event_close_at` field off the original webhook payload (see `Webinar Ended (webhook)`'s own notes for the full payload contract) and computes `sla_hours_after_close = (now − event_close_at) / 3600000`. Guards for the field being missing or unparseable (emits `null` + a note, doesn't throw).
2. **Build 3 Demo Send Records** (Code) — emits exactly 3 items, one per segment (`attendee`, `no_show`, `speaker`). Pulls a real attendee/no-show contact off `Shape M1 Payload`'s output when available, with a hardcoded fallback otherwise; the speaker segment always uses a placeholder record since this n8n lane doesn't currently ingest speaker data. Resolves a best-effort HubSpot `contact_id` for the association block downstream.
3. **Send Demo Email (Gmail)** — **native** `n8n-nodes-base.gmail` node (not generic HTTP), so Kai gets a proper OAuth2 "Gmail account" credential picker in the n8n UI instead of a raw bearer-token field.
4. **Log to HubSpot (email engagement)** — generic HTTP Request node, `POST /crm/v3/objects/emails`, body shape mirrored from `modules/m1-enrichment/push_to_hubspot.py`'s `build_email_log_entries()`. Uses a **Custom Auth** generic credential (`genericAuthType: httpTemplatedCustomAuth` — verified against the real node type definition; this is the JSON-template-with-`{{api_key}}`-placeholder type, not the older plain `httpCustomAuth` type the original task brief assumed).

### The override-inbox / `.example` demo design

This repo's real fixture data has attendee/no-show emails at real-looking (but fake) domains, and speaker emails at the `.example` TLD (RFC 2606, guaranteed non-routable — used deliberately so nobody accidentally emails a real inbox from a demo run). Regardless of which fixture addresses are technically routable, **this dispatch always sends to 3 fixed override inboxes** instead of any fixture address:

- `kai8karma+attendee@gmail.com`
- `kai8karma+noshow@gmail.com`
- `kai8karma+speaker@gmail.com`

The *intended* fixture recipient stays visible in every email: each body opens with a line like `[DEMO — would send to: tivanov@everlinetech.com]`, and the SLA figure from step 1 above appears as a footer line (`Sent 2.3h after event close.`). This means the demo is safe to actually fire (it can never spam a real prospect) while still being legible about who it *would* have gone to in production.

### Full credentials table

| # | node(s) | credential type | needed for |
|---|---|---|---|
| 1 | `Fetch Registrants` | none wired on purpose | stand-in webinar-platform endpoint (see its notes); point at your platform's registrants API + header auth when you have a real account |
| 2 | `Check Already Processed (HubSpot Search)` | **HubSpot App Token** (native node, `authentication: appToken`) | idempotency guard — a DIFFERENT n8n credential type than #3, even though both wrap the same private-app token string |
| 3 | `HubSpot Upsert Companies`, `HubSpot Upsert Contacts`, `Associate Contacts to Companies` | **HubSpot Header Auth** (generic HTTP, `Authorization: Bearer <token>`) | batch CRM writes (the native node has no v3 batch/upsert or v4 batch-association operation) |
| 4 | `Send Demo Email (Gmail)` | **Gmail OAuth2** *(new)* | the actual dispatch send — see design above |
| 5 | `Log to HubSpot (email engagement)` | **HubSpot Custom Auth** (`httpTemplatedCustomAuth`) *(new)* | logging each send as a CRM email engagement |
| 6 | `Push to Clay Table` | Clay table webhook source ID (URL param, not a credential object) | enrichment |
| 7 | `Notify Team of Failure` (Slack, disabled) | Slack bot token | optional failure ping |
| 8 | `Notify Team` (Telegram, disabled) | Telegram bot token | **optional** completion ping — the dispatch lane above is fully honest and complete without this node ever firing |

### How to create + attach each credential (n8n UI)

For every row above except #1 and #6 (which aren't credential objects, just URL/auth params to fill in directly on the node):

1. Open the workflow in the n8n editor (**master.json** → workflow id `rxJnu5ZATDFLJToD`).
2. Click the node that needs the credential (its **notes** field on the canvas names the exact credential type and placeholder id it expects).
3. In the node's parameter panel, find the **Credential** dropdown for that credential type (e.g. "Gmail account", "HubSpot App Token account", "Header Auth account", "Custom Auth account").
4. Click **Create New Credential**.
5. Fill in the fields for that credential type:
   - **HubSpot App Token**: paste your HubSpot private-app token.
   - **HubSpot Header Auth**: header name `Authorization`, value `Bearer <your private-app token>`.
   - **HubSpot Custom Auth** *(new — for node 5)*: n8n's Custom Auth (Templated) credential form — enter a JSON template such as `{"headers":{"Authorization":"Bearer {{api_key}}"}}` and fill in `api_key` with your HubSpot private-app token in the credential's own field.
   - **Gmail OAuth2** *(new — for node 4)*: click **Sign in with Google** and authorize the Gmail account you want sends to originate from (`kai8karma@gmail.com`, most likely).
   - **Slack** / **Telegram**: bot token from the respective app's developer settings.
6. Click **Save**.
7. n8n attaches the new credential to that node automatically once saved from within the node's panel; repeat for every other node that shares the same credential type (e.g. all 3 nodes in row #3 share one Header Auth credential — create it once, then pick the same saved credential from the dropdown on the other two nodes).
8. `Send Demo Email (Gmail)` currently starts **disabled** on the live instance specifically (n8n refuses to publish/activate a workflow with a native node missing its required credential, and none existed yet at the time of this update) — after attaching the Gmail OAuth2 credential, re-enable the node (right-click → **Activate**, or the toggle in the node's top-right corner). The repo file itself keeps this node enabled by default, since the repo JSON isn't subject to n8n's live publish-time credential check.

None of the above is required to run `demo-runner.json` — that remains the entire point of the demo lane.
