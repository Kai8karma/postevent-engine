# M4 — Lead Intelligence Dashboard

Local build (offline, stdlib only, contacts from --enriched's CSV):
    python3 build_dashboard.py --enriched <hubspot_ready.csv> --engagement ../../data/fixtures/engagement.json --segments ../../data/fixtures/segments.json --out dist/
    open dist/index.html

Live build (contacts read from HubSpot instead of the CSV — see "Input source" below):
    python3 build_dashboard.py --hubspot --enriched <hubspot_ready.csv> --engagement ../../data/fixtures/engagement.json --segments ../../data/fixtures/segments.json --out dist/

Deploy the narrative API to Vercel (dashboard works fully offline without this step):
    npm i -g vercel        # one-time
    cd modules/m4-dashboard
    vercel link && vercel env add ANTHROPIC_API_KEY
    vercel --prod

Env var required: `ANTHROPIC_API_KEY` (console.anthropic.com), or set `OPENROUTER_API_KEY`
instead — `api/narrative.js` calls OpenRouter's chat-completions API with the same
prompt/contract when `ANTHROPIC_API_KEY` is unset. `OPENROUTER_MODEL` overrides the
default model id (falls back through `anthropic/claude-sonnet-5` → `4.6` → `4.5` on
400/404 model errors). Without either key, or if `/api/narrative` is unreachable/slow
(client gives it 25s, then aborts), the dashboard renders the embedded
`fallback_narrative.md` instead — the page never ships blank. Verified live 2026-08-24
(no ANTHROPIC_API_KEY, real OPENROUTER_API_KEY, via `api/_local_invoke.js`): the
default paid chain (`anthropic/claude-sonnet-5`) returned a clean 200 with two
well-formed paragraphs in ~10s; a free-tier model (`OPENROUTER_MODEL` override) also
returned 200 in ~30s, though small/free models are less reliable at the strict-JSON
contract than the default Claude chain. Fixed as part of that verification pass:
`callOpenRouterModel` was requesting `max_tokens: 4000` unconditionally, which
OpenRouter hard-rejects with a 402 the moment the account can't afford the full
ceiling even if it can afford the actual answer (~700 tokens) — lowered to 800 to
match what a two-paragraph narrative actually needs.

## Input source — HubSpot vs. fixture

The spec's M4 input is "HubSpot data on event contacts" — `--hubspot` reads contacts
live via `POST /crm/v3/objects/contacts/search` filtered on `event_tag`, the same
Bearer-token idiom `modules/m1-enrichment/push_to_hubspot.py` uses (that script is
what put the contacts there in the first place — this is the same API in reverse,
verified against the same event_tag the live sandbox push used:
`acmerevenue-2026-07-20`). `--enriched`'s CSV is still required even with `--hubspot`
— its directory is also where `dedupe_report.json` / `quality_report.json` live
(alias canonicalization and completeness KPIs are local-run artifacts HubSpot doesn't
store, so those stay sourced from the CSV's sibling files regardless of where the
contact rows themselves came from). `--hubspot` degrades to the CSV on no token
found, a network/HTTP error, or zero search results — never a hard failure. The
dashboard's footer and the `source` key in the embedded JSON always say which lane
actually produced a given build.

Token resolution: env `HUBSPOT_TOKEN`, else `~/.config/postevent/hubspot.env`
(`HUBSPOT_TOKEN=...`) — same file `push_to_hubspot.py` reads. Never printed.

## AI boundary — what's a model call here and what isn't

Every number on this dashboard up through account/contact scoring, the funnel,
completeness percentages, and lifecycle movement is plain deterministic arithmetic
over the input rows, and has to reconcile exactly with what HubSpot itself would
report for the same data — no model, statistical or generative, ever touches a
count. Two places carry real judgment instead of counting:

- **Anomaly threshold** (`compute_anomaly_threshold` in `build_dashboard.py`): what
  counts as "anomalous" engagement used to be a hardcoded `>=15 events/day` guess
  with no relationship to the data it screened. It's now a Tukey IQR outlier fence
  (`threshold = ceil(Q3 + 1.5×IQR)`) computed fresh from *this run's own*
  distribution of per-contact event totals, with the computed `threshold_rationale`
  (median/Q1/Q3/IQR and the resulting number) embedded in the output JSON and shown
  on the page above the anomaly cards. This is a **statistical model, not a
  generative-AI call** — deliberately: "how unusual is this count, for this event"
  is a distribution-shape question with one mechanically correct answer, not a
  question needing linguistic judgment, and this file's contract is zero network
  calls by default. Forcing an LLM call into what is actually still counting would
  contradict the assignment's own principle ("AI must be the engine, not a wrapper")
  in the other direction — busywork dressed as AI.
- **Narrative summary** (`api/narrative.js`): the one real LLM call in this module —
  a fresh two-paragraph GTM-analyst read of the computed stats, regenerated on
  demand. This is where "narrate stage movement" (the spec's actual AI-role text for
  M4) lives.

Lead-interest scoring (`WEIGHTS` in `build_dashboard.py`) stays a fixed, auditable
weighted sum by design, not an oversight — a score a RevOps user can't hand-verify
against the raw event list, or that changes on every run for the same input, is
worse than a simple deterministic one for this use case.
