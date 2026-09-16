# M4 — Lead Intelligence Dashboard

HubSpot in, dashboard out. `dashboard.py` runs in four phases — `seed`, `sync`, `analyze`,
`render` — and the portal is the source of truth: M1 pushed this event's contacts (tagged with
the `postevent_event` property), M2 logged the email engagements as CRM `emails` objects, and M4
writes the engagement stream and the lifecycle changes **into** HubSpot before reading anything
back. Live is the default lane; `--offline` is the labelled fixture lane.

## 1. What M4 produces

| Brief output | Where it lands |
|---|---|
| Attendee → MQL conversion | `analysis.json` → `deterministic.mql_rate` + `attendee_to_mql {attendees, mqls}`; funnel + KPI widgets on the page |
| Top engaged accounts and contacts | `deterministic.top_accounts` / `top_contacts` (weighted engagement: form fill 10, click 3, pageview 1, open 0.5) |
| 7 / 14 / 30-day lifecycle movement | `deterministic.movement` — transitions counted from each contact's `lifecycle_history`, each window stating its own `source_mix` (`seeded` vs `hubspot_history`) |
| AI narrative, refreshed on load | `analysis.movement_narrative` + `narrative_source`; the page re-fetches it from `window.NARRATIVE_ENDPOINT` on load when the module API injects one |
| Anomalies with their evidence | `deterministic.anomalies` + the LLM's rationale per anomaly; the anomaly panel prints the evidence rows |
| Buying-committee map | `deterministic.committees` (≥2 engaged contacts at one company), with the LLM's "why" when the live lane ran |

## 2. How the AI is the engine — and the maths is the validator

Live `analyze` spends up to `--budget` (default 3) OpenRouter calls over `snapshot.json`, with
prompts built from real snapshot rows (`prompts/*.md`, no hard-coded companies):

1. `anomalies_and_scores.md` — judges the outlier fence's shortlist (accept, reject with a
   reason, or add one it missed) and scores every engaged contact 0–100 with a rationale and the
   evidence values behind it.
2. `movement_narrative.md` — narrates what moved across 7 / 14 / 30 days, what did not, and where
   the movement is seeded rather than organic.
3. `committees.md` — decides which multi-contact accounts are real buying committees and why.

Nothing the model returns overwrites a number. `build_validator()` compares its top-5 contacts,
top-5 accounts, committee list, anomaly list and window counts against the deterministic block and
writes every mismatch to `analysis.llm.validator.disagreements` (plus any contact it scored that
is not in the snapshot). The dashboard prints that panel. Deterministic-only pieces: the outlier
fence (Tukey Q3 + 1.5×IQR over this run's own distribution), the weighted engagement score, the
funnel, the movement windows, and a rules lead-interest score (0–100, normalised) so the rules
lane still fills that brief cell.

With `--offline` or no `OPENROUTER_API_KEY`, `analyze` writes `lane: "rules"`, `llm: null`, and a
`movement_narrative` templated from the deterministic counts — labelled `narrative_source: "rules"`
in the file, in `dashboard_data.json` and on the page's badge. It never pretends to be a model.

## 3. Phases and CLI

```
python3 modules/m4-dashboard/dashboard.py <phase> --out <dir> \
    [--event data/incoming/event.json] [--event-tag <slug>] \
    [--offline] [--live-dry-run] [--budget N]

phases: seed | sync | analyze | render | all
```

| phase | live behaviour |
|---|---|
| `seed` | matches portal contacts (search on `postevent_event`, paged) to this event's engagement stream by email, tries `POST /events/v3/event-definitions` once, and on a scopes refusal falls back to contact properties `postevent_opens/clicks/pageviews/form_fills` + `postevent_last_engaged` (created if missing, group `contactinformation`), then batch-updates ≤100 contacts per call with the totals and each contact's final `lifecyclestage`. Totals are **set**, never incremented: re-running yields the same values. |
| `sync` | contacts via search + `batch/read` with `propertiesWithHistory=lifecyclestage`; companies via associations + batch read; email engagements via contact→`emails` associations + batch read (subject, `hs_timestamp`). |
| `analyze` | deterministic block + the LLM pass above + the validator. |
| `render` | `index.html` (self-contained, zero network at rest) + `dashboard_data.json` — the exact rows the page draws. |

`--offline` is zero network: the snapshot is rebuilt from `data/fixtures/engagement.json`,
`hubspot_existing.json` and `segments.json`, every record labelled `source: "fixture"`, and region
and owner come from `config/icp.yaml`'s routing tables (the same ones M1 applies).
`--live-dry-run` prints every HubSpot request (method, URL, body size) and every prompt the live
lane would send, writes only `receipts/m4_dry_run.json` plus the prompts under `dry-run/`, sends
nothing and exits 0. Every phase's last stdout line is
`M4 <phase> (<lane>): <numbers> -> <out>`; any failure exits non-zero.

Env var names (read from the environment, else `~/.config/postevent/*.env`; never printed):
`HUBSPOT_TOKEN`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `LLM_BATCH_DEADLINE_S`, `LLM_REASONING`.

## 4. Files written under `--out`

| file | contents |
|---|---|
| `receipts/m4_seed.json` | `event_slug`, `lane`, `method` (`custom_events` / `contact_properties` / `offline`), `source: "seeded"`, `contacts_matched`, `events_written`, `lifecycle_updates`, `errors`, `endpoints`, `timestamps` |
| `snapshot.json` | `contacts` (ids, properties, `lifecycle_history` with per-row source, `engagement` counters with source), `companies`, `email_engagements`, `events`, `method`, `lane`, `notes` |
| `receipts/m4_hubspot_sync.json` | `endpoints` (method, url, pages, count, status) + `totals` |
| `analysis.json` | `deterministic`, `llm` (or null), `validator` inside `llm`, `movement_narrative`, `narrative_source`, `model`, `lane` |
| `receipts/m4_llm_calls.json` | one row per HTTP attempt: model, purpose, prompt chars, tokens, latency, HTTP status |
| `dashboard_data.json` | exactly what the page renders; every number reconciles to `snapshot.json` |
| `index.html` | the dashboard, data embedded, no external assets |

## 5. Seeded data — what is real and what is not

The registrant people for this demo are synthetic (real employer domains, invented humans), so
their post-event opens, clicks, pageviews, form fills and stage changes did not happen by
themselves: `seed` writes that stream into HubSpot through the public API, and HubSpot's own
property history then holds the movement. Everything that came from it is labelled
`source: "seeded"` in the seed receipt, on every snapshot engagement block, inside each movement
window's `source_mix`, and on the page's badges. What is not seeded: the contacts and companies
themselves (M1's push), the email engagements (M2's real sends), the CRM property history
(HubSpot's), and every number on the dashboard (computed from what the API returned).

## 6. Tests

```
python3 modules/m4-dashboard/test_dashboard.py     # 33 tests, stdlib unittest, no network
python3 -m py_compile modules/m4-dashboard/dashboard.py
```

`urllib.request.urlopen` is replaced module-wide with a raiser, so a test that reached for the
network fails instead of calling HubSpot or OpenRouter. Covered: snapshot shape from the fixtures,
the movement windows including the boundary instant, the committee rule, the MQL rate, a planted
validator disagreement, the rendered page (every widget id, the source badge, and the embedded
JSON equalling `dashboard_data.json`), `--live-dry-run` printing requests and prompts while
sending nothing, and the live `seed` / `sync` / `analyze` / `render` paths driven against an
in-memory fake portal (403 fallback, idempotent re-seed, history/company/email reads, receipts).

## 7. How the module API and n8n call it

`api/server.py` shells out to this CLI once per phase and reads the files above — it never
recomputes a number — then serves `GET /dashboard/<run_id>/` with
`window.NARRATIVE_ENDPOINT = "/narrative/<run_id>"` injected before the page's first `<script>`
tag, which is what turns on refresh-on-load. `GET /narrative/<run_id>?refresh=1` re-runs `analyze`
and answers `{narrative, source, generated_at, model, …}`; on any failure the page keeps the
narrative it was rendered with and its badge. Contract: [`docs/module-api.md`](../../docs/module-api.md)
(§"M4 — phases and files"). The n8n workflow that drives the schedule is
`orchestrator/n8n/railway/m4-lead-intelligence.json` — see
[`orchestrator/n8n/railway/README.md`](../../orchestrator/n8n/railway/README.md).
