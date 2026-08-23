# M4 narrative endpoint — live execution log

Event tag: `acmerevenue-2026-07-20` ("Pipeline After the Webinar: Turning Event
Engagement into Revenue"). Goal: prove `modules/m4-dashboard/api/narrative.js`
actually round-trips to a real model, using the dashboard's real computed
stats, not invented data.

## Request payload

Built from the real DATA block embedded in `out/live-proof/m4/index.html`
(`#dashboard-data`), reduced through the exact same `summaryPayload()`
transform the dashboard client uses (template.html). Saved at
`out/live-proof/m4-narrative/request_payload.json`. Real numbers: 133
registrants, 74 attendees, top account Palmetto SaaS Group (score 201.5, 9
engaged contacts, 90% committee coverage), 5 buying-committee accounts, 2
anomaly headlines.

## Harness

`modules/m4-dashboard/api/_local_invoke.js` — requires `narrative.js`
directly, builds a minimal fake `(req, res)` (`req.body` pre-set like
Vercel's Node runtime does, `res.status().json()` captured), times the call,
and prints `{ http_status, elapsed_ms, body }` as JSON. Never prints the key;
key is sourced into the subprocess env only.

## Runs

Provider: OpenRouter (`ANTHROPIC_API_KEY` unset, `OPENROUTER_API_KEY` sourced
from `~/.config/postevent/llm.env`). Command pattern:

```
cd "/Users/kiran/Desktop/Claude GOD/career/postevent-engine"
set -a; source ~/.config/postevent/llm.env; set +a
unset ANTHROPIC_API_KEY
export OPENROUTER_MODEL="<model id>"
node modules/m4-dashboard/api/_local_invoke.js out/live-proof/m4-narrative/request_payload.json
```

| # | model | http_status | elapsed_ms | source | fits 25000ms client timeout? |
|---|---|---|---|---|---|
| 1 | `nvidia/nemotron-3-ultra-550b-a55b:free` | 200 | 29869 | live | **no** (+4.9s over) |
| 2 | `nvidia/nemotron-3-ultra-550b-a55b:free` | 200 | 35226 | live | **no** (+10.2s over) |
| 3 | `nvidia/nemotron-3-super-120b-a12b:free` | 200 | 17086 | live | **yes** (7.9s headroom) |

Run 1's body is saved verbatim as `narrative_live.json` (the canonical proof
artifact for the model named in the task spec). Raw per-run JSON (status +
elapsed + body) is in `run1_ultra_raw.json`, `run2_ultra_raw.json`,
`run3_super_raw.json` alongside this log for audit.

No run failed, was retried past the built-in 429/5xx retry, or fell back to
a different model in `OPENROUTER_MODEL_FALLBACKS` — every run's requested
model answered on the first attempt. No stderr output on any run.

## Response-shape check

`narrative.js`'s output for all 3 runs: `{ paragraphs: [string, string],
generated_at: ISO8601, source: "live" }` — exactly what
`modules/m4-dashboard/template.html`'s `renderNarrative(paragraphs, source,
generatedAt)` expects (`template.html` reads `json.paragraphs`,
`json.generated_at`, and badges on `source`). **No contract mismatch found.
`narrative.js` was not modified.**

## Content spot-check

Run 1 paragraph 1 opening (first ~15 words): "The webinar drew 133
registrants and 74 attendees (55.6% attendance), yielding 31…" — matches the
real KPI numbers in the payload (133 / 74 / 55.6% / 31), not invented ones.
Paragraph 1 also correctly names the real top account, "Palmetto SaaS
Group," with its real score (201.5) and coverage (90%).

Paragraph 2 (anomalies) describes both anomalies accurately by the numbers
given (21/26 touches, 80.8%, 2026-08-07; 17/27 touches, 63.0%, 2026-07-25)
but does **not** name a contact or company for either — because the
`summaryPayload()` transform in `template.html` sends only `anomaly.headline`
strings into the `anomalies` array, and those headline strings (as computed
upstream, e.g. by M1/M3) do not themselves contain a contact/company name.
`SYSTEM_PROMPT` in `narrative.js` asks the model to name contact/company for
anomalies, but the data it's given doesn't carry those fields — so the model
correctly said "(company not disclosed)" rather than inventing a name. This
is a **data-payload gap upstream of narrative.js**, not a response-shape
contract bug, so it was left alone per the "only touch narrative.js if step
4 finds a contract bug" scoping — flagging it here for whoever owns the
anomaly-object schema.

## Latency vs. the dashboard's 25000ms timeout

`template.html` aborts the fetch at 25000ms and falls back to the cached
narrative labeled "Cached narrative." Measured against that:

- **`nvidia/nemotron-3-ultra-550b-a55b:free` (the model named in the task
  spec) is too slow for the client-side window** — 29.9s and 35.2s local,
  both already over 25s *before* adding real network hops (client → Vercel
  edge → OpenRouter → Nemotron backend → back). A hosted deployment would
  almost certainly time out and show the cached fallback for this model most
  of the time.
- **`nvidia/nemotron-3-super-120b-a12b:free` fits comfortably** — 17.1s
  local, ~7.9s of headroom before the 25s cutoff, and it's smaller/cheaper
  to run so hosted latency should track close to the local number.

**Recommendation:** deploy `OPENROUTER_MODEL=nvidia/nemotron-3-super-120b-a12b:free`,
not the ultra model, if "live narrative on load" inside the 25s window is
the goal. This is a deployment/config recommendation only — see DEPLOY.md.
No code was changed to make this call; it's a one-line env var choice.
