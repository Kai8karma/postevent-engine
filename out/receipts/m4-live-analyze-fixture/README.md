# M4 live-analyze receipt on the fixture snapshot (2026-09-17 ~00:05 IST)

What this is: `dashboard.py sync --offline` (snapshot = data/fixtures, 165 contacts, lane offline) followed by
`dashboard.py analyze` on the LIVE lane and `render`. It proves the LLM analysis path, the deterministic
validator and the fallback chain — not a portal read. The HubSpot-backed run replaces it once the 30-row
slice is pushed to the test portal.

Models: `OPENROUTER_MODEL=nvidia/nemotron-3-super-120b-a12b:free,google/gemma-4-31b-it:free,nvidia/nemotron-3.5-lightning:free`.
`receipts/m4_llm_calls.json` holds all 10 HTTP attempts: 4 completions, 3 provider 502s and 3 upstream 429s
before the third model answered the interest-scores prompt. `analysis.json.llm.validator` lists the four
fields where the model's ranking disagreed with the deterministic math; nothing was overwritten.
