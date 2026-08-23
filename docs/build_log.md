# Build Log

Disclosed on purpose — the assignment is being evaluated as much on *how* this
was built as on the artifact itself. Started as a skeleton and closed out at
ship on 2026-08-23; where a number was never metered it says so instead of
being invented.

Deadline: 2026-08-23 20:00 IST. Received: 2026-08-21 15:25 IST. Internal ship
target: 2026-08-23 ~14:00 IST.

## Agents used

Parallel build fleet, one Claude Code agent per row, each scoped to a single
directory (see `PLAN.md` → "Module owners").

All eight units were Claude Code subagents (Sonnet-class builders, dispatched
in parallel from one frontier-model main thread that did integration, the
judge panels and the fix waves). Token spend was not metered per unit — the
build ran on a flat-rate seat, so no per-call bill exists to quote; the
per-event *run* cost is estimated below and the live lane ran at $0.
Wall-clock = first file written → last edit (later edits are fix waves, not
the initial build, which closed within ~2h of dispatch for every unit).

| unit | scope | agent / model | wall-clock (IST) | token spend | human touchpoints |
|---|---|---|---|---|---|
| FIX-A | webinar content fixture (transcript, speakers, event) | Claude Code subagent | 08-21 15:47 → 08-23 14:33 (date-shift pass) | not metered | 0 |
| FIX-B | registrant + CRM + engagement fixtures | Claude Code subagent | 08-21 15:47 → 08-22 17:41 (webinar-#2 fixture) | not metered | 0 |
| M1 | `modules/m1-enrichment/` | Claude Code subagent | 08-21 16:48 → 08-23 16:52 (HubSpot injector fixes) | not metered | Clay workspace auth + credit cap; HubSpot sandbox/app/token |
| M2 | `modules/m2-comms/` | Claude Code subagent | 08-21 15:56 → 08-23 16:46 (OpenRouter backend) | not metered | approval gate (by design, never auto-flipped) |
| M3 | `modules/m3-repurpose/` | Claude Code subagent | 08-21 15:55 → 08-23 16:46 (OpenRouter backend) | not metered | 0 |
| M4 | `modules/m4-dashboard/` | Claude Code subagent | 08-21 16:39 → 08-23 16:46 (OpenRouter backend) | not metered | Vercel deploy go/no-go (Kai) |
| ORCH | `orchestrator/` (run_pipeline.py + n8n JSONs) | Claude Code subagent | 08-21 15:55 → 08-23 16:59 (backend-aware live banner) | not metered | n8n Cloud instance + workflow import (Kai, via UI) |
| DOCS | `docs/` (this control room) | Claude Code subagent | 08-21 16:47 → 08-23 ship | not metered | ship-time review by Kai |

Loop used: builders run in parallel and self-verify → a critic pass per module
→ fix pass → integration check via `orchestrator/run_pipeline.py`.

## Wall-clock

- Assignment received: 2026-08-21 15:25 IST
- Build fleet dispatched: 2026-08-21 ~15:45 IST (first fixture/module files
  land 15:47–16:48)
- First integration run (`run_pipeline.py --out out/final`, offline): 2026-08-21
  17:14 IST — green on the first attempt, no seam fixes needed. Total pipeline
  wall-clock 0.45s (`time python3 orchestrator/run_pipeline.py`).

  | stage | seconds | result |
  |---|---|---|
  | M1 enrich | 0.16 | PASS — 150 input rows, 3 fake-email exclusions, 133 output rows |
  | M2 comms | 0.08 | PASS — 3 email variants rendered, 153 recipients |
  | M3 repurpose | 0.07 | PASS — 4 content assets |
  | M4 dashboard | 0.07 | PASS — 10 top accounts, 5 buying-committee accounts, 2 anomalies |

- Judge panel round 1 (3 adversarial personas): 2026-08-21 evening → fix wave 1
- Judge panel round 2 (depth-focused): 2026-08-22 → fix wave 2
- Live lanes: Clay enrichment live 2026-08-23 (run `run_0tk7y5kH2Y5PHTW5moE`,
  1.5 credits); HubSpot sandbox push live 2026-08-23 ~16:45–17:00 IST
  (30 companies / 133 contacts / 120 associations, zero errors); LLM live
  proof 2026-08-23 17:00–18:30 IST via OpenRouter (`out/live-proof/`; M1 121 s,
  M3 548 s, M2 see `run-m2.log`)
- Looms: scripts in `docs/loom-scripts.md`; recorded by Kai after this pass
  (links go into the submission email if done before send — if not, the
  control room + zip stand on their own)
- Final ship: 2026-08-23 (target was ~14:00 IST; actual close-out ran to the
  evening because the live HubSpot/LLM lanes were prioritised over an early
  send)

## Token spend

- Per-module estimates used for `economics.md`: M1 ~40k, M2 ~15k, M3 ~60k,
  M4 ~5k tokens (120k/event, offline fixture run — no `--live` calls made
  during the timed estimate).
- Actual `--live` spend: the proof run in `out/live-proof/` went through
  OpenRouter on `nvidia/nemotron-3-ultra-550b-a55b:free` (first attempts on
  `stealth/ox-alpha` were abandoned: ~2 min/call and it spent its whole
  completion budget reasoning on the transcript prompts). Both are priced at
  $0 on OpenRouter at time of run — `usage.cost: 0` in every response.
  Per-stage wall-clock is in each `run*.log` receipt table; raw model
  responses M2 received are under `m2/live_raw/`. Build-time generation of
  the cached offline outputs ran under a flat-rate seat and was not metered.

## Human touchpoints

Every judgment call a human made mid-build, not just the ship-time approval
gate baked into M2:

- Company/webinar identity: fixed by the recruiter's brief (ACME Revenue
  Cloud / synthetic-but-realistic, since the hiring company's real webinar was
  not named before the deadline — see punch list item 1 in `PLAN.md`).
- Manual overrides during build (ICP tier, email copy, content draft edits):
  none — fixtures are synthetic and no real send occurs before Kai reviews
  this control room. Every change came through the critic → fix-wave loop.
- Account-side actions (human-only by policy — the agent never holds a
  credential it didn't need to): Clay workspace sign-in + "preserve credits"
  cap on live runs; HubSpot developer account, sandbox portal, private-app
  creation and token paste; OpenRouter key + model choice; n8n Cloud sign-up
  and workflow import via UI.
- M2 approval gate: stays `approved:false` until Kai flips it — email
  engagements are logged to HubSpot only after that.
- Pre-submission: Kai reviews all four modules + this control room before
  recording Looms and sending `submission_email.md`.

## What's real vs. simulated

- Real: the code, the prompts, the offline pipeline run, the n8n workflow
  JSON (cloud lane imported into a live n8n Cloud instance), the architecture
  and economics reasoning, the live Clay enrichment run on real domains, the
  live HubSpot push into a provisioned sandbox (private app, 15 scopes), and
  the live LLM proof run in `out/live-proof/`.
- Simulated: the webinar itself (synthetic transcript/speakers/registrants —
  see `PLAN.md` punch list item 1). The HubSpot portal is a sandbox, not a
  customer's; Clay ran on three real domains only (credit-capped), the
  fixture's synthetic domains return nothing from Clay by construction.
