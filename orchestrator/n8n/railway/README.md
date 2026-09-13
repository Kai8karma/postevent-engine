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

`validate.py` → **GREEN, 0 failures, 3 warnings**:
- 2× orphan warnings on the sticky notes (expected — sticky notes never have incoming connections)
- 1× `${...}` warning inside `Build Evidence Summary`'s `jsCode` — false positive, that's a real
  JS template literal (`` `${summary.run_id}.json` ``) inside a Code node, not an n8n expression
  field.

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
