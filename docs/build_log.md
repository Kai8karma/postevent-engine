# Build Log — superseded (v1)

This file documented the v1 build (Claude Code subagent fleet, wall-clock,
token-spend estimates, "what's real vs simulated") for the submission a
Darwinbox reviewer rejected on 2026-09-12 for not following the case study.

v2 is a different build against the same four brief modules, orchestrated by
n8n (Railway) calling a small module API in front of the Python engine. Its
build history lives as a running progress log, not a closed-out narrative:

- `_internal/REVISION-PLAN-2026-09-12.md` — the plan plus a dated progress
  log (what shipped each session, commit hashes, what's still Kai-gated).
- `docs/module-api.md` — the current module contract (phases, request/
  response shapes, receipts).
- `modules/*/README.md` — each module's own "what's real vs simulated"
  section (e.g. `modules/m4-dashboard/README.md` §5).

Don't cite this file's numbers (token spend, wall-clock, the old fixture's
row-by-row dedupe breakdown) — they describe a different build on a
different fixture.
