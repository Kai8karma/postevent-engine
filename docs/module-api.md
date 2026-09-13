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
