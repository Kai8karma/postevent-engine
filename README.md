# Post-Event Engine

Working prototype for AI-led post-event content automation, built against the
assignment brief in [`SPEC.md`](SPEC.md). Four independent modules — lead-list
enrichment (M1), post-event communications (M2), content repurposing (M3),
and a lead-intelligence dashboard (M4) — chained by one orchestrator. Every
module runs two honestly-labeled lanes: **offline** (default, zero network,
replays AI output generated at build time so a reviewer can see the full
pipeline in seconds with no keys) and **`--live`** (real LLM calls at
runtime, via an authenticated `claude` CLI or an OpenRouter key — this is
the "AI is the engine" lane the brief asks for; every live receipt in this
build actually ran through OpenRouter, not `claude -p`). The terminal and
the dashboard both print/label which lane produced what you're looking at;
nothing pretends a cached sample is a live call.

## Run it yourself

- **Live console app:** **https://postevent-engine.vercel.app** — drop in
  your own registrant CSV or transcript, hit Run, watch each module execute
  for real against the bundled fixture or your own input. The write-up and
  full evidence trail (this repo's `docs/`) is hosted at
  [`/control-room`](https://postevent-engine.vercel.app/control-room) on the
  same deployment.
- **Offline, no keys, ~1 second:**
  ```
  python3 orchestrator/run_pipeline.py
  ```
- **Live, real LLM calls, at no cost (needs a free OpenRouter key):**
  ```
  LLM_BACKEND=openrouter \
  OPENROUTER_MODEL="nvidia/nemotron-3-super-120b-a12b:free,nvidia/nemotron-3-ultra-550b-a55b:free" \
  OPENROUTER_MAX_TOKENS=4000 \
    python3 orchestrator/run_pipeline.py --live --out out/my-live-run
  ```
  Every model in that chain is a `:free` slug, so a live run costs nothing.
  Measured 2026-08-24: M1 finishes in ~2m20s and adjudicates all 9 dedupe
  gray-zone pairs with no parse failures. `nemotron-3.5-lightning:free` is
  faster still but returns JSON that does not match the batch schema, so it
  is deliberately not first in the chain.
- **Clone it:**
  ```
  git clone https://github.com/Kai8karma/postevent-engine && cd postevent-engine
  ```

## Start here

Hosted control room (write-up + evidence): **https://postevent-engine.vercel.app/control-room**
(M4 dashboard at `/dashboard/`). The live lane has already been run end to
end against a real model endpoint — the receipt is in `out/live-proof/`
(per-stage logs, the regenerated M1/M2/M3 outputs, and `run*.log` receipt
tables) — and the HubSpot sandbox push, Clay enrichment, Sarvam
transcription, live image generation, shared-drive upload, and M4 narrative
receipts are all on the control room's "Live receipts" section, scored
against the brief in its "Scored against the brief" table. The exact command
to reproduce the live lane yourself is in "Run it yourself" above.

Two more one-pagers, linked from the control room's "The numbers" section:
[`docs/data-contract.md`](docs/data-contract.md) — every field this engine
writes, its source, type/enum, derivation, and what happens when it's
missing, in one place instead of scattered across four files — and
[`docs/operating-cadence.md`](docs/operating-cadence.md) — how this runs
week to week once it's live: the review cycle, who owns the M1/M2 review
queues, the same-day SDR handoff SLA, and what's honestly not built yet for
10x scale (a cross-event contact ledger, send-fatigue suppression — neither
exists today).

## Depth, ranked honestly

The brief says attempt all four modules, depth of execution matters more
than breadth — all four were attempted; here's the honest ranking of how
deep each one goes, in order:

1. **M1 — deepest.** Real dedupe math (a stated weighted composite score,
   not a black box — [`data-contract.md`](docs/data-contract.md) §2), a live
   Clay enrichment run and a live HubSpot sandbox push (not a dry-run), and
   completeness measured against the brief's own named fields, not an
   inflated denominator (99.8% contact / 97.2% company, both above the 90%
   bar).
2. **M3 — next.** A real extraction-to-asset chain — one `claude -p`
   extraction pass over the transcript, then every asset (blog/YouTube/
   infographic/social) generated off that extraction rather than each
   guessing independently off the raw transcript — and a grounding verifier
   (`modules/m3-repurpose/verify_grounding.py`) that checks every timestamp,
   quote, and speaker attribution each asset claims against the source
   transcript, writing a per-asset pass/fail `grounding_report.json`.
3. **M2 — strong, deliberately stopped.** Real content generation and real
   CRM send-log wiring, stopped short of an actual send by the human
   approval gate (`approval_gate.json`, never auto-flipped) — a design
   choice, not an unfinished feature.
4. **M4 — best engineering, thinnest AI, by choice.** One live narrative
   call; anomaly detection and lead-interest scoring are deterministic
   rules, not LLM calls. Reproducibility is a compliance property here, not
   a missed opportunity: an SDR disputing a merge a year from now needs a
   byte-identical replay of how that account got scored, and a deterministic
   rule gives them that where a model call wouldn't.

## Quickstart (offline, no keys, ~1 second)

The offline lane replays cached model output so the whole pipeline is
inspectable in a second with zero keys. M1's offline path is deterministic
rules only (no cached LLM output) — its LLM inference runs in `--live`; the
thesis table in the control room says exactly which lane is AI where.

```
git clone <repo-url> && cd postevent-engine   # or: unzip the submission zip && cd into it
python3 orchestrator/run_pipeline.py
open out/<event-slug>/m4/index.html && open docs/index.html
```

`run_pipeline.py` prints a per-stage receipt table (pass/fail, seconds, key
output) but not the final path — `<event-slug>` is `event.json`'s
`event_name`, lowercased and hyphenated. With the bundled fixture event
that's `out/pipeline-after-the-webinar-turning-event-engagement-into-revenue/`
(verified by running it). `docs/index.html` is the judge control room:
per-module demo links, architecture diagram, economics table, build log.

## Live mode

```
./orchestrator/demo.sh          # judge-facing: tries --live, falls back to offline LOUDLY if auth is missing
python3 orchestrator/run_pipeline.py --live
```

The `--live` lane works with **either** an authenticated `claude` CLI **or**
an OpenRouter key — it is not a `claude -p`-only lane, and in fact every live
receipt on the control room's "Live receipts" section ran through OpenRouter,
because `claude -p` auth was unavailable on the build machine the day those
receipts were produced. M1/M2/M3 read `LLM_BACKEND` (`auto`/`claude`/
`openrouter`, default `auto` — tries `claude -p` first, falls back to
OpenRouter if a key exists) and `OPENROUTER_API_KEY` (env, or
`~/.config/postevent/llm.env`); M4's narrative endpoint falls back to
`OPENROUTER_API_KEY` the same way when `ANTHROPIC_API_KEY` isn't set on
Vercel. If you do have an authenticated `claude` CLI on PATH (`claude
login`, or `ANTHROPIC_API_KEY` exported), that satisfies the `claude -p` path
instead — `./orchestrator/demo.sh` checks specifically for that and only
takes the live branch if it finds it; if you're running the OpenRouter path
instead, skip `demo.sh` and call `run_pipeline.py --live` directly (see "Run
it yourself" above) so you're not relying on a claude-only auth check.
`ANTHROPIC_API_KEY` is a separate, optional requirement for M4's Vercel
narrative endpoint only (see module map below); the dashboard renders a
baked-in fallback narrative without it, so nothing about the core pipeline
needs that key. See each module's README for the exact env vars.
`OPENROUTER_MODEL` picks the model (pass it explicitly — each module's
default is a paid Claude model); the shipped live proof in `out/live-proof/`
was run against a single pinned model, not a fallback chain:

```bash
LLM_BACKEND=openrouter OPENROUTER_MODEL=nvidia/nemotron-3-ultra-550b-a55b:free \
  python3 orchestrator/run_pipeline.py --live --out out/live-proof
```

(An earlier attempt on `stealth/ox-alpha` was abandoned first — see
[`build_log.md`](docs/build_log.md) — and is not part of this command.) For a
faster run today, lead with the newer, much faster free model instead — see
the live command under "Run it yourself" above, which chains
`nemotron-3.5-lightning` → `nemotron-3-super-120b` → `nemotron-3-ultra-550b`
so a slow or overloaded provider degrades gracefully instead of hanging.

`OPENROUTER_MODEL` takes a comma-separated chain (M2/M3 hand off to the next
model when a provider is overloaded or rate-limited after retries). Free and
reasoning-style models take 30 s–2 min per call, wrap JSON in fences with a
trailing sentence, and sometimes spend the whole completion budget thinking —
the modules' JSON extraction, timeouts, retries and shape validation are sized
for exactly that, so expect a live run to take minutes, not seconds, and read
`out/<run>/<module>/_stage.log` if a stage fails.

**`OPENROUTER_MAX_TOKENS` is worth setting.** The default is 12000, sized for
reasoning models that think before emitting JSON. On OpenRouter that ceiling
costs wall-clock even when the answer is short — measured against
`nemotron-3.5-lightning:free`, an identical two-token reply took **50.7s at
12000, 29.4s at 6000, and 8.0s at 1500**. Latency tracks the ceiling, not the
response. 4000 is the sweet spot for the free models above: enough headroom
for a 30-row JSON batch, several times faster than the default. It also
matters on a low balance — OpenRouter checks affordability against the
ceiling rather than actual usage, returning `402 ... can only afford N` on a
request that would have cost a fraction of that. `enrich.py` retries smaller
on a 402 rather than reporting the run as out of credits.

Two `--live` backends exist and `LLM_BACKEND=auto` (the default) prefers the
first: an authenticated `claude` CLI, or OpenRouter. If you have a Claude
subscription, `claude login` is the higher-quality path and costs nothing
extra.

## Module map

| Module | Does | README |
|---|---|---|
| M1 — Lead List Enrichment | Fuzzy-dedupes a messy registrant export against HubSpot, infers missing fields, ICP-tiers with rationale, routes region/owner | [`modules/m1-enrichment/README.md`](modules/m1-enrichment/README.md) |
| M2 — Post-Event Communications | Renders attendee/no-show/speaker emails, UTM-tagged, gated behind a human approval flag | [`modules/m2-comms/README.md`](modules/m2-comms/README.md) |
| M3 — Content Repurposing | Transcript → blog draft, YouTube chapters/description/thumbnail brief, infographic outline, 8 social posts | [`modules/m3-repurpose/README.md`](modules/m3-repurpose/README.md) |
| M4 — Lead Intelligence Dashboard | Self-contained HTML dashboard: conversion, top accounts, stage movement, AI narrative (with offline fallback) | [`modules/m4-dashboard/README.md`](modules/m4-dashboard/README.md) |
| Orchestrator | Chains M1→M2→M3→M4, per-event partitioning, n8n workflow JSONs | [`orchestrator/README.md`](orchestrator/README.md) |

## Run it on your own webinar

1. Replace the four files in `data/incoming/` (`event.json`,
   `registrants.csv`, `speakers.json`, `transcript.md`) with the real
   event's data, in the same shape.
2. Edit `config/icp.yaml` — company name/domain and the ICP tier rules
   (titles, company size, industries) for the hiring company.
3. Run `python3 orchestrator/run_pipeline.py --live` (or `./orchestrator/demo.sh`).
   Output lands in `out/<new-event-slug>/` — automatically partitioned, it
   won't clobber the bundled demo event's output.
4. Review `out/<slug>/m2/approval_gate.json` and the M1 `needs_review` rows
   before treating anything as send-ready — nothing auto-sends.

## n8n: two lanes

Two separate n8n workflow sets exist under `orchestrator/n8n/` — a local-demo
lane that shells out to these same CLI scripts, and a production-shaped cloud
lane wired to real HubSpot/Clay/webhook nodes with credential placeholders.
Full explanation, import instructions, and the honesty caveats (what's been
verified by inspection vs. actually imported into a running n8n) live in
[`orchestrator/README.md`](orchestrator/README.md) — don't assume either lane
without reading it first.

## Requirements

Python 3.9+, standard library only. No `pip install`, no `requirements.txt`,
no accounts needed for the offline lane. `--live` needs either an
authenticated `claude` CLI **or** an `OPENROUTER_API_KEY` — not `claude`
specifically; M4's optional narrative endpoint needs `ANTHROPIC_API_KEY` or
`OPENROUTER_API_KEY` on Vercel (see
[`modules/m4-dashboard/README.md`](modules/m4-dashboard/README.md)).

## Live-run prerequisites

None of the above are needed for the offline lane — it's what every quickstart
command on this page runs. These only matter for wiring the production path
(real HubSpot/Clay/n8n, not the demo scripts standing in for them):

- **HubSpot private app** with scopes `crm.objects.contacts.read/write`,
  `crm.objects.companies.read/write`, `crm.schemas.contacts.read/write`,
  `crm.schemas.companies.read/write`, `marketing.campaigns.read/write`,
  `marketing-email` — covers M1's contact/company upsert
  ([`modules/m1-enrichment/clay_spec.md`](modules/m1-enrichment/clay_spec.md))
  and M2's send/campaign wiring
  ([`modules/m2-comms/hubspot_wiring.md`](modules/m2-comms/hubspot_wiring.md)).
- **n8n reachable at a public URL** (a tunnel like `cloudflared` or `ngrok` in
  front of local `npx n8n`, or n8n Cloud) — Clay's enrichment callback and the
  human-approval webhook in
  [`orchestrator/n8n/cloud/master.json`](orchestrator/n8n/cloud/master.json)
  both resume a `Wait` node via an inbound webhook, which only works if n8n
  has a real inbound URL, not `localhost`.
- **Clay account with credits**, plus CLI/webhook access to push the
  registrant export in and pull enriched rows out — see
  [`modules/m1-enrichment/clay_waterfall_recipe.json`](modules/m1-enrichment/clay_waterfall_recipe.json)
  for the exact table/column/credit shape to build.
- **Authenticated `claude` CLI** (`claude login`) **or** `OPENROUTER_API_KEY`
  set (or `ANTHROPIC_API_KEY` for the `claude` path without running `claude
  login`) — required for every `--live` flag across M1/M2/M3 and for M4's
  Vercel narrative endpoint. Every live receipt this build actually shipped
  ran the OpenRouter path.
- **Drive credentials (optional)** — only needed to automate M3's shared-drive
  upload; `repurpose.py` itself doesn't call Drive. A real upload has been
  proven manually (7 files, event-tag-prefixed, see the control room's
  "Live receipts" section and `out/live-proof-drive/`); the exact
  request/endpoint shape a production integration would call is in
  [`scripts/publish_to_drive.md`](scripts/publish_to_drive.md).
