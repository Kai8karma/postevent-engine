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
