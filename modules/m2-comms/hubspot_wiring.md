# Superseded — M2 HubSpot wiring (v1 document)

This file described the v1 build, where `comms.py` stopped at files on disk and no code
path in the repo called a real email or CRM API. That is no longer true and this document
is kept only so old links resolve.

In the current build M2 runs live by default: `comms.py` generates the variants from the
real transcript with a grounding verifier, and `log_dispatch.py` writes real HubSpot email
engagements (CRM v3 `emails`, association type 198, idempotent on message id).

Current sources of truth:

- [`README.md`](README.md) — what this module does and how to run both lanes.
- [`../../docs/module-api.md`](../../docs/module-api.md) — section M2: the `generate`,
  `approve` and `log` phases, with the dispatch-plan and results schemas.
- [`../../orchestrator/n8n/railway/README.md`](../../orchestrator/n8n/railway/README.md) —
  section M2: the workflow that drives those phases.
- `../../out/receipts/m2-live/` — the receipts from the live run.
