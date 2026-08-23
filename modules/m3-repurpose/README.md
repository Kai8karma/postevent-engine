# M3 — Content Repurposing

Transcript in, multi-format content package out: blog draft, YouTube chapters/description/thumbnail brief, infographic outline, 8 social posts.

`python3 repurpose.py --out out/m3` runs offline (default): copies `sample_output/` into `--out`, stamps each file with an event tag (`<!-- event: acmerevenue-2026-08-19 | generated: offline-sample -->`), writes `manifest.json`.

`python3 repurpose.py --transcript data/incoming/transcript.md --event data/incoming/event.json --out out/m3 --live` runs the real pipeline: one `claude -p` extraction pass (`prompts/extraction.md`) over the transcript, then one `claude -p` call per asset (`prompts/blog.md`, `youtube.md`, `infographic.md`, `social.md`) filled with `{{TRANSCRIPT}}`, `{{EVENT_JSON}}`, `{{EXTRACTION}}`.

`python3 repurpose.py --out out/m3 --live-dry-run` builds all 5 real prompts (extraction + blog + youtube + infographic + social) with actual transcript/event data filled in and writes them to `out/m3/dry-run/*.prompt.md` — zero network calls, `claude -p` is never invoked. Use this to verify the `--live` path is wired correctly when auth is unavailable. The 4 asset prompts fill `{{EXTRACTION}}` with a labeled placeholder since the extraction call itself is skipped for the same zero-network reason.

`sample_output/` is the quality bar the live path is expected to hit, not a placeholder — every quote, timestamp, and stat in it is pulled straight from `data/incoming/transcript.md`.

`--live` fails loud on any problem — missing `claude` binary, timeout, non-zero exit (including auth failures) — with a clear stderr message and exit 1, rather than silently writing partial or no output.

`--live` LLM backend: `LLM_BACKEND` env selects `auto` (default, tries `claude -p` then falls back to OpenRouter if a key exists), `claude`, or `openrouter`. OpenRouter key comes from `OPENROUTER_API_KEY` env, else a `OPENROUTER_API_KEY=...` line in `~/.config/postevent/llm.env` (never printed). `OPENROUTER_MODEL` overrides the default model id (falls back through `anthropic/claude-sonnet-5` → `4.6` → `4.5` on 400/404 model errors).

**Image-gen slots in production, not here.** This module ships specs, never pixels: `youtube.md`'s Thumbnail Brief (composition/text overlay/colors) and `social.md`'s quote-card posts are both handoffs to an image-generation step downstream — thumbnail art and branded quote-card graphics respectively. Production wiring: an n8n node reads that brief/quote text and calls an image API (e.g. `generate_image`), writes the asset back to the shared drive next to the markdown, and a human approves before publish — same judgment-gate discipline as M2's sends.
