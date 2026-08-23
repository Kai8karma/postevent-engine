# api/run.py -- Vercel deploy notes

For the main thread wiring `orchestrator/stage_vercel.py` / `vercel.json`.

## Route

`api/run.py` exports a module-level `class handler(BaseHTTPRequestHandler)`
(the Vercel Python runtime's contract for a serverless function). Vercel maps
it to:

- `GET  /api/run` -- service status: `{ok, repo_root_ok, service, live_available, model_chain, modules, error?, llm_liveness_probe?}`.
  `ok` **is** `repo_root_ok` -- it reflects whether this deployment actually
  bundled `modules/`/`config`/`data` next to `api/run.py` (see "What must
  ship next to this function" below), not a constant. `error` (the
  `REPO_ROOT_ERROR` string) is only present when `repo_root_ok` is false.
  `ok` is deliberately NOT downgraded by a bad/exhausted OpenRouter key --
  the offline lane works with no key at all, so a down LLM provider is not a
  broken deployment. `live_available` is `true` iff an OpenRouter key is
  present in this deployment's environment (`OPENROUTER_API_KEY`) -- lets the
  UI grey out the "live" toggle before the reviewer ever clicks Run. Pass
  `?probe=1` to also get `llm_liveness_probe: {ok, model, detail,
  checked_at}` -- a real 1-token OpenRouter completion call that surfaces the
  provider's own error text verbatim (e.g. a 429 body). This is opt-in *and*
  cached process-wide for `PROBE_CACHE_TTL_S` (300s) so repeated status polls
  never re-burn quota; every plain `GET` (no `probe` param) makes zero
  outbound calls. See "Live vs degraded lane" below for why this probe is the
  only place that provider text is recoverable at all.
- `POST /api/run` -- runs one module (or the full chain). Request/response
  JSON shape is specified in full in the workstream brief this file's sibling
  code was built against; not repeated here to avoid drift -- read
  `api/run.py`'s module docstring and `handle_run()` if you need the exact
  contract, it is the single source of truth. One field is called out here
  because it is the P0 fix this file documents: the response's `"lane"` is
  one of `"live" | "offline" | "degraded"`, set from evidence the module
  itself emitted (its own report file / stderr), never from the module's
  exit code alone. See "Live vs degraded lane" below.

## Live vs degraded lane (P0 fix)

Before this fix, `"lane"` was set purely from whether `live:true` was
requested -- if `enrich.py --live` silently fell back to the rule-table
result (its own preflight `check_llm_health()` found no usable backend, one
buried `[warn]` stderr line, exit 0 regardless), `api/run.py` still reported
`lane:"live"`. That is precisely the "fake AI" failure mode this build
exists to avoid, so it's now a third lane value:

- `"offline"` -- `live:false` (or no key present, downgraded before running).
- `"live"` -- `live:true`, and the module's own evidence proves real model
  output landed (m1: `live_inference_report.json` shows patched rows against
  attempted batches; m2/m3: the module exited 0 at all, since both fail loud
  -- `RuntimeError`, non-zero exit -- on any backend problem, see
  `comms.py`'s `_llm_call` / `repurpose.py`'s `call_llm` docstrings, so there
  is no silent-fallback path to misreport there).
- `"degraded"` -- `live:true` was requested, the module ran (exit 0), but no
  model output actually landed: zero rows patched with batches attempted, a
  parse-failure count equal to the batch count, or an explicit no-backend
  warning in stderr (m1's only failure mode today); or the module has no live
  capability at all (m4, which has no `--live` flag).

A degraded response's `notes` array carries the module's own verbatim
evidence, a plain-English gloss, and what the reviewer can do next (supply
their own key, or read `out/live-proof/`) -- see `degraded_note()` in
`api/run.py`. If an `OPENROUTER_API_KEY` is present, it also runs the
`probe_openrouter_liveness()` ping described above and appends the
provider's real error text, since `enrich.py`'s internal preflight discards
that detail before it ever reaches stderr.

Every `POST` response's `summary` also carries `llm_calls_made` (m1: LLM
batches attempted; m2: 3 iff all three segment caches regenerated live, else
0; m3: `1 + len(ASSETS)` iff extraction + every asset call succeeded, else
0) and, for m1, `llm_rows_patched` -- so the live claim is a number, not just
a status word.

## vercel.json requirement

```json
{
  "functions": {
    "api/run.py": { "maxDuration": 60 }
  }
}
```

Vercel Hobby plan caps serverless functions at 60s. `api/run.py` enforces its
own internal budget under that (offline lane: ~20s ceiling, effectively
finishes in well under 1s; live lane: 45s ceiling per module subprocess) so
the function always returns a clean JSON response -- `ok:true` or `ok:false`
-- before Vercel would kill it. Without `maxDuration:60` in `vercel.json`,
Vercel's Hobby default (10s) will kill live-lane requests outright with a
platform-level 504, which the UI cannot render nicely (no JSON body).

## What must ship next to this function

`api/run.py` resolves its repo root by searching upward from `__file__` for
a directory containing `modules/m1-enrichment/enrich.py`, then requires that
same root to also contain `config/` and `data/`. Concretely, the deployed
bundle must have, as siblings of `api/run.py`'s parent:

```
<deploy-root>/
  api/run.py
  modules/
    m1-enrichment/enrich.py   (+ prompts/, tools/ if present)
    m2-comms/comms.py         (+ prompts/, sample_output/, .fingerprint.json)
    m3-repurpose/repurpose.py (+ prompts/, sample_output/, tools/)
    m4-dashboard/build_dashboard.py (+ template.html, fallback_narrative.md)
  config/icp.yaml
  data/
    incoming/registrants.csv, event.json, transcript.md   (default fixture)
    fixtures/hubspot_existing.json, engagement.json, segments.json
```

As staged today (`orchestrator/stage_vercel.py`), `out/vercel-stage/.../modules/`
only carries each module's `*.md` docs -- the actual `.py` scripts and their
`prompts/`/`sample_output/`/`template.html`/fixture support files are not
copied. This function will 500 with a clear "cannot locate repo root" error
(or a `FileNotFoundError` from a missing prompt/template) until
`stage_vercel.py` is updated to copy the four directories above (the whole
`modules/` tree including scripts, plus `config/` and `data/`) into the
staged deploy root, not just the docs. `.pycache/` under `modules/**` can be
excluded; everything else the scripts read at runtime (prompts, templates,
fixture JSON, `.fingerprint.json`) must ship.

## requirements.txt

Present at the repo root, comments-only. No third-party packages -- `api/run.py`
and every module script it shells out to are stdlib-only. The empty file's
only job is to make Vercel detect a Python runtime for `api/run.py`.

## Local testing without the `vercel` CLI

```
python3 api/_local_serve.py 8787
curl -s http://localhost:8787/api/run
curl -s -X POST http://localhost:8787/api/run -H 'Content-Type: application/json' \
  -d '{"module":"m1","live":false}'
```

`_local_serve.py` mounts the same `handler` class Vercel would instantiate
directly on stdlib `http.server` -- it is dev tooling only and is never
itself deployed.

## Known live-lane reality (read before filing a "live is broken" issue)

Live-lane timings measured locally against the current OpenRouter free tier
(2026-08-23, model chain led by `nvidia/nemotron-3.5-lightning:free`):

- Full 150-row fixture, `enrich.py --live`: 5m24s.
- Minimal 2-row CSV (1 row needing LLM inference), `enrich.py --live`: still
  well over 60s.

The bottleneck right now is OpenRouter free-tier queueing/latency, not batch
count or the module code. That means a live single-module request can
plausibly time out even on the smallest possible input, under current
provider load. `api/run.py` handles this as designed: an honest `ok:false`
naming the module and elapsed time, with a hint pointing at the pre-computed
full live run already committed at `out/live-proof/`. This is expected
behavior, not a defect in the endpoint -- do not raise `LIVE_TIMEOUT_S` past
Vercel's 60s cap chasing it; there is no timeout value under that cap that
reliably succeeds at current OpenRouter latency.
