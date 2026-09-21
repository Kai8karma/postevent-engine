# Architecture

What feeds what, module by module, and how the pieces are wired together
today. Full endpoint/phase contract: [`docs/module-api.md`](module-api.md).
Deploy steps: [`docs/deploy-module-api.md`](deploy-module-api.md). n8n
workflow detail: [`orchestrator/n8n/railway/README.md`](../orchestrator/n8n/railway/README.md).

## Data flow

```mermaid
flowchart TD
    IN1["registrants.csv (150 rows, messy)<br/>+ config/icp.yaml"]
    IN2["event.json + speakers.json<br/>+ transcript.md (Sarvam STT)"]

    M1["M1 — Lead Enrichment<br/>dedupe (batch + HubSpot Search API) → LLM infer<br/>→ Clay firmographics → ICP score → push to HubSpot"]
    M2["M2 — Post-Event Comms<br/>extract + 3 segment variants<br/>→ approval gate → send → log engagement"]
    M3["M3 — Content Repurposing<br/>extract → blog/YouTube/infographic/social<br/>→ images + clips → Drive"]
    M4["M4 — Lead Intelligence<br/>seed engagement into HubSpot → sync back<br/>→ LLM analysis + validator → dashboard"]

    GATE{{"human approval gate<br/>(n8n Wait node) before any send"}}
    HS[("HubSpot: contacts, companies,<br/>email engagements, lifecycle history")]

    IN1 --> M1
    IN2 --> M2
    IN2 --> M3
    M1 -- "hubspot_ready.csv (dedup'd)" --> M2
    M1 --> HS
    M2 --> GATE --> HS
    M3 --> DRIVE["Google Drive folder<br/>Post-Event/&lt;event_slug&gt;/&lt;run_id&gt;"]
    HS --> M4
    M4 --> DASH["/dashboard/&lt;run_id&gt;/<br/>+ /narrative/&lt;run_id&gt;"]
```

Reading it: M1 is the only module that writes to HubSpot's contact/company
records — M2 and M4 both trust what it wrote rather than re-reading the raw
registrant list. M3 has zero dependency on M1/M2 — it works straight off
the transcript. M4 is the sink: it seeds this event's engagement stream
into HubSpot itself, then reads the portal back (M1's push, its own seed,
and whatever `emails` objects the portal holds), so it can't produce a
meaningful dashboard until M1 has run at least once. The M2 leg of that
read is empty in practice: **no email has been sent**, and the only
`emails` object in the committed snapshot is a logger probe.

## How it runs today

Live is the default lane end to end — every module calls its real backend
(HubSpot, Clay, OpenRouter, Sarvam) unless a run explicitly
asks for the offline lane. The Google Drive leg is wired but has never run:
no OAuth credential is connected and `manifest.json.shared_drive` is empty (`--offline` on the module CLIs, `"live": false`
on the module API). Offline replays a labelled fixture and makes zero
network calls; it's the fallback, not the demo default.

```mermaid
flowchart LR
    trig["webhook (per event: M1/M2/M3)<br/>+ schedule (M4, every 6h)"] --> n8n["n8n workflows<br/>orchestrator/n8n/railway/*.json<br/>self-hosted on Railway"]
    n8n -- "POST /run {module, phase}" --> api["module API<br/>api/server.py — stdlib, Railway"]
    api --> hs["HubSpot"]
    api --> clay["Clay (M1 firmographics)"]
    api --> or["OpenRouter (LLM + image gen)"]
    api --> sarvam["Sarvam STT (M3)"]
    n8n --> drive["Google Drive (M3 upload)"]
    api -. "GET /narrative?refresh=1" .-> vercel["Vercel: web/index.html console<br/>+ docs/index.html control room<br/>+ modules/m4-dashboard/api/narrative.js proxy"]
```

`api/server.py` is the one process that shells out to each module's real
script (`enrich.py`, `comms.py`, `repurpose.py`, `dashboard.py`) — n8n never
calls a module script directly, only this HTTP layer. The orchestrator this
package ships is self-hosted n8n on Railway — the four workflows under
`orchestrator/n8n/railway/` — and **none of them has been imported into a
running n8n instance**. For a walkthrough with no n8n at all, the local
equivalent of the same DAG is
`python3 orchestrator/run_pipeline.py --offline --out out/replay`.

Each module owns its own LLM-call helper rather than sharing one chokepoint
script. Primary path is OpenRouter (`OPENROUTER_API_KEY`, read from env or
`~/.config/postevent/llm.env`, never printed); M1 and M2 additionally try
`claude -p` first when `LLM_BACKEND=auto`, falling back to OpenRouter — both
strip `USER` from the subprocess environment before that call, since
`claude -p` 401s against keychain auth otherwise.
