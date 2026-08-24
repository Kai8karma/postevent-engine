# M4 — Lead Intelligence Dashboard

Local build (offline, stdlib only, contacts from --enriched's CSV):
    python3 build_dashboard.py --enriched <hubspot_ready.csv> --engagement ../../data/fixtures/engagement.json --segments ../../data/fixtures/segments.json --out dist/
    open dist/index.html

Live build (contacts read from HubSpot instead of the CSV — see "Input source" below):
    python3 build_dashboard.py --hubspot --enriched <hubspot_ready.csv> --engagement ../../data/fixtures/engagement.json --segments ../../data/fixtures/segments.json --out dist/

Live build with advisory annotations (optional LLM call over the already-computed
findings — see "Optional advisory annotation layer" below; requires OPENROUTER_API_KEY):
    python3 build_dashboard.py --enriched <hubspot_ready.csv> --engagement ../../data/fixtures/engagement.json --segments ../../data/fixtures/segments.json --out dist/ --live-annotations

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

### Optional advisory annotation layer (`--live-annotations`)

The spec's M4 AI-role bullet also asks the dashboard to "detect anomalies…, score
lead interest…, surface top accounts and buying committees" — work this module has
always done with the deterministic rule code above, on purpose (see "AI boundary"
above). `--live-annotations` adds a second, **opt-in** real LLM call
(`generate_live_annotations` / `attach_live_annotations` in `build_dashboard.py`)
that **annotates** those already-computed anomaly and top-account findings with a
one-line "why it matters / what an SDR should do" note — it never recomputes or
overrides a number. Same advisory-only shape as
`modules/m1-enrichment/enrich.py`'s `apply_icp_second_opinion()` (appends a
rationale string, never rewrites `icp_tier`): the model sees only a cheap
projection of the run's own anomaly candidates, top accounts, and lifecycle-window
stats — never the full contact roster — and every annotation is
**grounded-by-construction**: a post-check (`_grounded()`) rejects (and counts, in
`annotations_rejected`) any annotation that cites a number not already present
somewhere in the payload it was given, before it's ever attached. Output JSON gets
four new top-level fields regardless of whether the flag is passed —
`annotations_source` (`"llm_live"` | `"none"`), `annotations_model`,
`annotations_generated_at`, `annotations_rejected` — plus an `annotation` string on
individual `anomalies[]` / `top_accounts[]` entries that got one; the dashboard
renders these as a small italic line under the relevant card/row, and the
provenance next to the `engagement_source` label in the header, only when present
— absent (the default), the page renders exactly as it always has.

Off by default; requires `OPENROUTER_API_KEY` (env or
`~/.config/postevent/llm.env`, same read pattern as everywhere else in this repo).
Defaults to the README's free nemotron chain (`ANNOTATION_MODEL_FALLBACKS` —
`nemotron-3.5-lightning` → `nemotron-3-super-120b` → `nemotron-3-ultra-550b`, all
`:free`), not a paid model, since this is a new opt-in step rather than something
on the pipeline's critical path; `OPENROUTER_MODEL` still overrides/prepends.
Degrades to no annotations — never a hard failure — on a missing key, a network
error, an unparseable response, or a response that parses but has every annotation
grounding-rejected; each of those hands off to the next model in the chain before
giving up, not just an HTTP-level failure.

Verified live 2026-08-24 against this repo's own fixture-lane payload
(`out/final/m1/hubspot_ready.csv` + `data/fixtures/engagement.json` +
`data/fixtures/segments.json`, 2 anomaly candidates + 10 top accounts = 12 items):
the chain's first-choice model, `nemotron-3.5-lightning:free`, reliably failed
against this prompt's constraint density — either burning its whole completion
budget on a visible "Here's a thinking process:" preamble before ever emitting
JSON, or degenerating into a repetition loop (`finish_reason:"stop"`, zero valid
JSON) — so `generate_live_annotations()` now hands off to the next model on an
unparseable/unusable response, not only on an HTTP-level error (this file's
earlier version didn't, and silently produced `annotations_source:"none"` on a
technically-200 response). `nemotron-3-super-120b-a12b:free` and
`nemotron-3-ultra-550b-a55b:free` each answered cleanly in ~26s and ~86s
respectively — 12/12 items annotated, 0 rejected after a second live-discovered
fix: the grounding check originally compared numbers as literal strings, which
falsely rejected correct annotations that wrote a JSON `100.0`/`50.0` as natural
prose "100%"/"50%" (`"100" != "100.0"`) — `_numbers_in_text()` now parses to
`float` before comparing. Sample annotation (`nemotron-3-super-120b-a12b:free`,
grounded): *"Palmetto SaaS Group leads with a score of 201.5, 9 of 10 contacts
engaged and 90.0% committee coverage—prioritize multi-threaded outreach to close
the deal."* Both fixes are covered above; the checked-in `out/final/m4/index.html`
itself currently shows `annotations_source:"none"` because the account's shared
free-tier daily quota (50 requests/day, `openrouter_free_tier_daily`, one bucket
across every `:free` model, not per-model) was exhausted by this same debugging
pass before a final rebuild could complete — re-running the "Live build with
advisory annotations" command above after the daily reset (`X-RateLimit-Reset`,
UTC midnight) reproduces the live-populated result.
