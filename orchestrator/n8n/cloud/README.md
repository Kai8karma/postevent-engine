# orchestrator/n8n/cloud — two lanes

This directory ships two separate n8n workflows for the same post-event pipeline. They are independent files — import either one, or both, without conflict (different node names, no shared IDs).

| file | workflow name | purpose | credentials needed |
|---|---|---|---|
| `demo-runner.json` | Post-Event Engine — Cloud Demo (no credentials) | Proves the pipeline runs, green, end to end, on a stock n8n Cloud account with nothing configured. | **none** |
| `master.json` | Post-Event Engine — Cloud Production | The real GTM lane: webinar platform → Clay enrichment → HubSpot contacts/companies/associations → approval gate → HubSpot single-send → Telegram notify. | HubSpot private-app token, Clay table webhook ID, webinar-platform API, Telegram bot token (see below) |

Both lanes are driven from the same source of truth for what the pipeline actually does: `orchestrator/run_pipeline.py` and the four module scripts under `modules/`.

## Why two lanes

The previous state of this repo's n8n workflow was a single straight-line 13-node chain where everything past the first `Wait` node needed credentials nobody had wired — "not even a single complete run" in the executions list. `demo-runner.json` exists to answer that directly: it is a workflow a reviewer can execute right now, with zero setup, and watch complete. `master.json` is the real lane a production deployment would eventually run, once someone wires HubSpot/Clay/Telegram.

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

## How to import

1. In n8n: **Workflows → Import → Import from URL**.
2. Demo lane: `https://postevent-engine.vercel.app/n8n/demo-runner.json`
   Production lane: `https://postevent-engine.vercel.app/n8n/master.json`
3. **Gotcha hit tonight while building this**: n8n's Import does **not** replace the nodes already on the canvas — it appends the imported workflow's nodes on top of whatever is there. If you import into a canvas that already has a workflow open, you end up with duplicated nodes. **Clear the canvas first** (open a new/blank workflow, or select-all + delete) before importing, or import into a brand-new workflow.

## How to fire the demo

**Via the UI**: open `demo-runner.json` in the n8n editor and click **Execute Workflow** (uses the Manual Trigger branch). The result appears in the execution's output pane on `Merge Module Summaries` / `Respond: Success`.

**Via curl**, against the workflow's *test* webhook URL (visible in the n8n editor when you click the Webhook node and hit "Listen for Test Event", or the *production* URL once the workflow is Active):

```bash
curl -X POST "https://agentkai.app.n8n.cloud/webhook-test/postevent-demo" \
  -H "Content-Type: application/json" \
  -d '{
    "event_name": "Pipeline After the Webinar: Turning Event Engagement into Revenue",
    "live": false
  }'
```

Swap `webhook-test` for `webhook` once the workflow is Active, to hit the production webhook URL instead. Omit `registrants_csv` / `transcript_md` entirely (as above) to run against the bundled fixture — that's what a reviewer should do for the default demo run.

### What a reviewer should expect to see

- The execution goes fully green (no red nodes) in well under a minute.
- The final JSON response (from `Respond: Success`) contains `ok: true`, a `run_key`, and a `modules` object with `m1`/`m2`/`m3`/`m4`, each carrying that module's real `summary` (e.g. `m1.summary.contact_completeness_pct`, `m4.summary.attendee_to_mql_pct`).
- If `contact_completeness_pct` happens to fall under 90 on a given run, the execution still completes — check the `Flag: Below SDR-Handoff Bar` node's output for the flag, and `modules.m1.summary` in the final response for the actual number.
- Killing network access to `postevent-engine.vercel.app` (or pointing the URL at something unreachable) should route the run into `Engine API Unreachable` and stop with a clear error message, not hang.

## master.json — production lane — what still needs wiring

This is the other agent's workstream (`orchestrator/n8n/cloud/master.json`) — documented here for completeness since a reviewer will look at both files together. Nodes that need a real credential or endpoint before this lane can complete a run:

| node | needs |
|---|---|
| `Fetch Registrants` | a real webinar-platform registrants API + auth (currently `authentication: none`, stand-in URL) |
| `Push to Clay Table` | a real Clay table webhook source ID (currently `PLACEHOLDER_CLAY_TABLE_WEBHOOK_ID`) |
| `Wait for Clay Enrichment Callback` | Clay table configured to call this node's resume-webhook URL when enrichment finishes (table-level "run complete" webhook, not per-column) |
| `HubSpot Upsert Contacts`, `HubSpot Upsert Companies`, `Associate Contacts to Companies`, `HubSpot Single Send` | a Header Auth credential (`Authorization: Bearer <HubSpot private-app token>`), referenced today as placeholder `PLACEHOLDER_HUBSPOT_HEADER_AUTH` |
| `Wait for Human Approval` | whatever UI/process posts the approval callback to this node's resume-webhook URL |
| `Notify Team (disabled)` | a Telegram bot credential (`PLACEHOLDER_TELEGRAM_BOT_CRED`) — also currently disabled on the canvas |

None of that is required to run `demo-runner.json` — that is the entire point of the demo lane.
