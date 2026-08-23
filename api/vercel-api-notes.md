# api/run.py -- Vercel deploy notes

For the main thread wiring `orchestrator/stage_vercel.py` / `vercel.json`.

## Route

`api/run.py` exports a module-level `class handler(BaseHTTPRequestHandler)`
(the Vercel Python runtime's contract for a serverless function). Vercel maps
it to:

- `GET  /api/run` -- service status: `{ok, service, live_available, model_chain, modules}`.
  `live_available` is `true` iff an OpenRouter key is present in this
  deployment's environment (`OPENROUTER_API_KEY`) -- lets the UI grey out the
  "live" toggle before the reviewer ever clicks Run.
- `POST /api/run` -- runs one module (or the full chain). Request/response
  JSON shape is specified in full in the workstream brief this file's sibling
  code was built against; not repeated here to avoid drift -- read
  `api/run.py`'s module docstring and `handle_run()` if you need the exact
  contract, it is the single source of truth.

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
