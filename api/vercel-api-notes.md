# api/run.py -- Vercel deploy notes

For the main thread wiring `orchestrator/stage_vercel.py` / `vercel.json`.

`api/run.py` is still live: `api/server.py` (the Railway module-API
service, see `docs/module-api.md`) imports it directly (`import run as
legacy`) for its generic subprocess-runner and per-module summarize/
artifact helpers, and `orchestrator/stage_vercel.py` still stages it as its
own Vercel serverless function. Two different Vercel-facing things exist
side by side now -- this file is about the older one, `POST`/`GET
/api/run`. The console's live narrative refresh goes through a separate,
newer function; see "The narrative endpoint" below.

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
  outbound calls.
- `POST /api/run` -- runs one module (or the full chain) by shelling out to
  that module's real script (`enrich.py`, `comms.py`, `repurpose.py`, and
  -- see "Retired" below -- an M4 entry point that no longer exists).
  Request/response JSON shape: read `api/run.py`'s module docstring and
  `handle_run()`, not repeated here to avoid drift. One field is called out
  because it is the P0 fix this file documents: the response's `"lane"` is
  one of `"live" | "offline" | "degraded"`, set from evidence the module
  itself emitted, never from the exit code alone -- see "Live vs degraded
  lane" below.

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
their own key, or read the committed receipts under `out/receipts/`) -- see `degraded_note()` in
`api/run.py`. If an `OPENROUTER_API_KEY` is present, it also runs the
`probe_openrouter_liveness()` ping described above and appends the
provider's real error text, since `enrich.py`'s internal preflight discards
that detail before it ever reaches stderr.

## Retired: this endpoint no longer runs modules

M1/M2/M3 flipped from offline-by-default (v1) to **live-by-default**, with `--offline`
as the explicit opt-out, and M4 moved from a single script to a phased CLI. This file's
`stage_m1`/`stage_m2`/`stage_m3`/`stage_m4` builders still spoke v1's convention, which
no longer round-trips:

- **M1** never added `--offline` on the `live:false` path, so every run went live
  whichever lane the caller asked for.
- **M2** was passed `--live` or `--allow-stale`; `comms.py` has neither flag today, so
  it exited on an unrecognised argument on every call.
- **M3** ran live regardless of the requested lane, for the same reason as M1.
- **M4** invoked a script that no longer exists, with flags that no longer exist.

A run endpoint that reports the wrong lane is worse than no run endpoint, so
`handle_run()` now returns a refusal naming the replacement instead of executing
anything. The v1 body is kept as `_handle_run_v1_disabled()` for reference and is not
reachable. The supported entry point is the module API in `api/server.py`
(`POST /run` with a module and a phase) — see `docs/module-api.md` and
`docs/deploy-module-api.md`.

`api/run.py` itself stays: `api/server.py` imports it for `REPO_ROOT`, `run_module`,
`build_env`, the per-module summarisers and the artifact specs. Only the endpoint is
closed.

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
    m3-repurpose/repurpose.py (+ prompts/, sample_output/)
    m4-dashboard/dashboard.py (+ template.html) -- see "Retired" above:
      not actually reachable through this function's current M4 stage
  config/icp.yaml
  data/
    incoming/registrants.csv, event.json, transcript.md   (default fixture)
    fixtures/hubspot_existing.json, engagement.json, segments.json
```

`orchestrator/stage_vercel.py` already copies the whole `modules/`,
`config/` and `data/` trees (`.pycache/` excluded) into the staged deploy
root, so this list is met mechanically today -- the gap is in `api/run.py`'s
own M4 stage (above), not in what gets shipped.

## The narrative endpoint (a separate function, not this one)

The M4 dashboard's live-refreshing narrative does **not** go through
`api/run.py`. It's `modules/m4-dashboard/api/narrative.js`, a second, much
simpler Vercel function: a pure proxy that forwards `GET /api/narrative?
run_id=<id>[&refresh=1]` to the module API's own `GET /narrative/<run_id>`
(Railway, `api/server.py`), attaches the bearer token server-side, and
returns that response verbatim. It makes no model calls of its own and
needs no model-provider key on Vercel -- only `MODULE_API_URL` and
`MODULE_API_TOKEN` as project env vars. Full contract:
`docs/deploy-module-api.md` (§9) and `docs/module-api.md`.

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

Timing numbers from an earlier measurement (2026-08-23, against a model
chain that is no longer the current default) aren't repeated here, since
nothing has re-timed a live call through this specific endpoint since the
module CLIs moved to live-by-default -- see "Retired" above, which is
the more urgent caveat: for M2 and M4, and for M1/M3's offline path, this
endpoint may not reach a model call at all today. Where it does reach one,
OpenRouter free-tier queueing/latency is still the practical bottleneck
(see each module's own `receipts/*_llm_calls.json` for real per-call
latency from its own live runs). `api/run.py` handles a timeout as
designed either way: an honest `ok:false` naming the module and elapsed
time, never a bare platform 504 -- do not raise `LIVE_TIMEOUT_S` past
Vercel's 60s cap chasing this.
