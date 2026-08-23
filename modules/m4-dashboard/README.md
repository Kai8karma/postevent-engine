# M4 — Lead Intelligence Dashboard

Local build (offline, stdlib only):
    python3 build_dashboard.py --enriched <hubspot_ready.csv> --engagement ../../data/fixtures/engagement.json --segments ../../data/fixtures/segments.json --out dist/
    open dist/index.html

Deploy the narrative API to Vercel (dashboard works fully offline without this step):
    npm i -g vercel        # one-time
    cd modules/m4-dashboard
    vercel link && vercel env add ANTHROPIC_API_KEY
    vercel --prod

Env var required: `ANTHROPIC_API_KEY` (console.anthropic.com), or set `OPENROUTER_API_KEY`
instead — `api/narrative.js` calls OpenRouter's chat-completions API with the same
prompt/contract when `ANTHROPIC_API_KEY` is unset. `OPENROUTER_MODEL` overrides the
default model id (falls back through `anthropic/claude-sonnet-5` → `4.6` → `4.5` on
400/404 model errors). Without either key, or if `/api/narrative` is unreachable/slow
(>3s), the dashboard renders the embedded `fallback_narrative.md` instead — the page
never ships blank.
