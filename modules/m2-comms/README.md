# M2 — Post-Event Communications

Renders the attendee / no-show / speaker thank-you emails and gates every send behind human approval.

- `python3 comms.py --out <dir>` — offline, reads cached `sample_output/*.json` takeaways.
- `python3 comms.py --out <dir> --enriched <hubspot_ready.csv|.json> --live` — live copy via `claude -p`.
- Falls back to `data/incoming/registrants.csv` when `--enriched` is missing (M1 not run yet).
- Output: `emails/*.md` (3 variants, UTM-tagged), `sends_log.json` (HubSpot-shaped), `approval_gate.json` (blocks all sends until a human sets `approved: true`).
- Prompts in `prompts/`, production send path in `hubspot_wiring.md`.
- `--live` LLM backend: `LLM_BACKEND` env selects `auto` (default, tries `claude -p` then falls back to OpenRouter if a key exists), `claude`, or `openrouter`.
- OpenRouter key: `OPENROUTER_API_KEY` env, else a `OPENROUTER_API_KEY=...` line in `~/.config/postevent/llm.env`; never printed.
- `OPENROUTER_MODEL` overrides the default model id (falls back through `anthropic/claude-sonnet-5` → `4.6` → `4.5` on 400/404 model errors).
