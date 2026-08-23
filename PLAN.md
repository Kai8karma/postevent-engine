# Build Plan — Post-Event Engine

Strategy: exceed spec on six axes (the punch list), build offline-first so nothing gates on accounts/keys, swap in live services as they arrive.

## Punch list (differentiators)

1. **Their-own-webinar fixture** — pipeline runs on the hiring company's real public webinar. BLOCKED on company name; until then everything runs on a synthetic-but-realistic B2B webinar. Swap = replace `data/incoming/` files + `config/icp.yaml`.
2. **One-command end-to-end** — `python3 orchestrator/run_pipeline.py data/incoming/` chains M1→M2→M3→M4 locally. n8n workflow JSONs mirror the same DAG as the production path.
3. **Judge control room** — `docs/index.html`: one URL, per-module live demo + 90s Loom + architecture diagram + economics line.
4. **Unit economics table** — cost per event (LLM + Clay credits) vs manual hours saved.
5. **Judgment gates** — human-approval node before sends, confidence + rationale column on every enriched row, no blind auto-send.
6. **Disclosed meta-flex** — build log (agents, hours, tokens); submission email drafted by Module 2 itself.

## Architecture decisions (fixed — do not relitigate in agents)

- **Offline-first**: every module runs from `data/fixtures/` with zero network. `--live` flag switches to real LLM calls via `claude -p` (pattern: `scripts/lib/claude_call.py` at workspace root — strip `USER` env or keychain auth 401s).
- **Python stdlib only** (csv, json, difflib, re, argparse) — no pip installs, runs on any Mac.
- **Runtime LLM prompts** live in each module's `prompts/` dir as .md files; pre-generated sample outputs live in each module's `sample_output/`. Both ship: prompts prove the engine, samples prove the output bar.
- **Dashboard**: single self-contained `index.html`, inline SVG charts (no CDN), data embedded as JSON block, `/api/narrative.js` Vercel serverless function + cached fallback narrative baked in. Theme: clean B2B, works offline.
- **n8n**: workflow JSONs in `orchestrator/n8n/`, valid importable schema. Local n8n via `npx n8n` (no account needed). n8n Cloud optional later.
- **HubSpot/Clay**: demo path = local scripts emulating the operations + property-mapping docs; production path = documented Clay table spec + HubSpot private-app wiring guide. Honest two-lane framing in control room.

## Module owners (agent fleet)

| unit | builds | key output |
|---|---|---|
| FIX-A | webinar content: transcript (~8k words, 3 speakers, named B2B SaaS topic), speaker list, session metadata | `data/incoming/transcript.md`, `speakers.json`, `event.json` |
| FIX-B | registrant mess: 150-row Zoom-export CSV (dupes, casing chaos, freemail, missing fields), attendance segments, engagement events, HubSpot contact fixtures | `data/incoming/registrants.csv`, `data/fixtures/*.json` |
| M1 | enrich.py (fuzzy dedupe difflib, field inference prompts, ICP scoring w/ rationale, region/owner routing), Clay spec doc | `modules/m1-enrichment/` |
| M2 | comms.py + 3 email variants ×2 subject lines each, approval gate, UTM conventions, HubSpot logging spec | `modules/m2-comms/` |
| M3 | repurpose.py + blog draft, YT chapters/description/thumbnail brief, infographic outline, 8 social posts | `modules/m3-repurpose/` |
| M4 | dashboard index.html + narrative serverless fn + fallback | `modules/m4-dashboard/` |
| ORCH | run_pipeline.py end-to-end + n8n workflow JSONs (master + per-module) | `orchestrator/` |
| DOCS | control room index.html, mermaid architecture diagrams, economics table, build log skeleton | `docs/` |

Loop: builders (parallel, self-verifying) → ruthless critic per module → fix pass → integration check.

## Kai's parallel homework (cannot be done by Claude)

- [ ] Company name → their public webinar URL (unlocks punch #1)
- [ ] HubSpot free dev account (developers.hubspot.com) — self-serve signup
- [ ] Clay free trial — self-serve signup
- [ ] n8n: NOT needed as account — local `npx n8n` works; Cloud signup optional
- [ ] ANTHROPIC_API_KEY (console.anthropic.com) — for Vercel narrative fn only
- [ ] Sunday: record 5× 90s Looms off the control room script
