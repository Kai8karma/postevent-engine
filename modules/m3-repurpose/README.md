# M3 — Content Repurposing

Transcript in, multi-format content package out: blog draft, YouTube chapters/description/thumbnail brief, infographic outline, 8 social posts.

`python3 repurpose.py --out out/m3` runs offline (default): copies `sample_output/` into `--out`, stamps each file with an event tag (`<!-- event: acmerevenue-2026-08-19 | generated: offline-sample -->`), writes `manifest.json`.

`python3 repurpose.py --transcript data/incoming/transcript.md --event data/incoming/event.json --out out/m3 --live` runs the real pipeline: one `claude -p` extraction pass (`prompts/extraction.md`) over the transcript, then one `claude -p` call per asset (`prompts/blog.md`, `youtube.md`, `infographic.md`, `social.md`) filled with `{{TRANSCRIPT}}`, `{{EVENT_JSON}}`, `{{EXTRACTION}}`.

`python3 repurpose.py --out out/m3 --live-dry-run` builds all 5 real prompts (extraction + blog + youtube + infographic + social) with actual transcript/event data filled in and writes them to `out/m3/dry-run/*.prompt.md` — zero network calls, `claude -p` is never invoked. Use this to verify the `--live` path is wired correctly when auth is unavailable. The 4 asset prompts fill `{{EXTRACTION}}` with a labeled placeholder since the extraction call itself is skipped for the same zero-network reason.

`sample_output/` is the quality bar the live path is expected to hit, not a placeholder — every quote, timestamp, and stat in it is pulled straight from `data/incoming/transcript.md`.

`--live` fails loud on any problem — missing `claude` binary, timeout, non-zero exit (including auth failures) — with a clear stderr message and exit 1, rather than silently writing partial or no output.

`--live` LLM backend: `LLM_BACKEND` env selects `auto` (default, tries `claude -p` then falls back to OpenRouter if a key exists), `claude`, or `openrouter`. OpenRouter key comes from `OPENROUTER_API_KEY` env, else a `OPENROUTER_API_KEY=...` line in `~/.config/postevent/llm.env` (never printed). `OPENROUTER_MODEL` overrides the default model id (falls back through `anthropic/claude-sonnet-5` → `4.6` → `4.5` on 400/404 model errors).

**Image-gen slots in production, not here.** This module ships specs, never pixels: `youtube.md`'s Thumbnail Brief (composition/text overlay/colors) and `social.md`'s quote-card posts are both handoffs to an image-generation step downstream — thumbnail art and branded quote-card graphics respectively. Production wiring: an n8n node reads that brief/quote text and calls an image API (e.g. `generate_image`), writes the asset back to the shared drive next to the markdown, and a human approves before publish — same judgment-gate discipline as M2's sends.

## Grounding verification

`prompts/extraction.md` and the asset prompts all demand grounding — verbatim quotes, real `[MM:SS]` timestamps, correct speaker attribution — but until now nothing checked it. `verify_grounding.py` does, and `repurpose.py` runs it automatically after every generation, in **both** lanes (offline replay and `--live`):

- every `[MM:SS]` / `[H:MM:SS]` timestamp cited (including bare leading timestamps in `youtube.md`'s `## Chapters` list, and both ends of a `[MM:SS-MM:SS]` range) must be a real turn-start time in the transcript, or one of the transcript's own declared segment boundaries;
- where a citation pairs a name with a timestamp (`Daniel Kim, Northwind Analytics [05:10]`), the transcript speaker who actually has the turn at that moment must match;
- every quoted string above `MIN_QUOTE_CHARS` (25 chars) that's attributed to a named speaker must fuzzy-match (`difflib.SequenceMatcher` ratio ≥ 0.90) some transcript window — the same primitive `modules/m1-enrichment/enrich.py`'s dedupe already uses;
- every `— Name, Title, Company` attribution must name someone in the event's actual speaker list.

Output is `<out>/grounding_report.json`: per-asset claims checked/verified/failed, each failure with the offending text, best-matching transcript window, and its ratio.

**Annotate, don't block, by default.** This is a human-reviewed content pipeline — a blog draft and social copy headed for a review queue, not an automated publish. The default behavior is to prepend a visible `> GROUNDING CHECK FLAGGED ...` banner to any asset that failed (naming each failed claim) and print a one-line summary to stdout — loud enough that a reviewer can't miss it, without blocking the whole batch from ever reaching them over one bad quote. Pass `--strict-grounding` to instead fail the run (exit 1) — for a CI check or a publish gate that wants the harder failure.

```bash
python3 repurpose.py --out out/m3                    # generates + auto-verifies + annotates
python3 repurpose.py --out out/m3 --strict-grounding  # same, but exit 1 on any failed claim
python3 verify_grounding.py --transcript data/incoming/transcript.md \
    --event data/incoming/event.json --assets-dir out/m3 --strict  # standalone, e.g. re-check an existing --out
```

**Real result against the live-generated assets** (`out/live-proof/m3/`, real `claude -p` output, checked against `data/incoming/transcript.md`; see `out/live-proof-grounding/grounding_report.json`): 66/68 claims verified. Two failed, both genuine near-misses rather than fabrications — no invented quote, timestamp, or speaker attribution was found:
- `blog.md`: a pull-quote attributed to Sara Alvarez ("That number reflects a program we've iterated on for a while. A first webinar with none of that infrastructure in place is going to see a much smaller multiple, and that's fine.") silently drops a middle clause and softens the ending versus the real line at `[54:50]` — best ratio 0.746, below the verbatim bar.
- `social.md`: a rhetorical aside ("Most teams treat \"follow up within 24 hours\" as a win") sits close enough to a Daniel Kim mention to register as an attributed quote-claim; it isn't actually presented as something he said, so this is a heuristic false positive, not a real quality issue.

## UTM tagging

Every outbound link in `blog.md`, `youtube.md`, and `social.md` (not `infographic.md` — its CTA text is a design mockup, not a publishable link) carries a UTM query string. `campaign_slug()` / `slugify()` in `repurpose.py` are a byte-for-byte mirror of `modules/m2-comms/comms.py`'s functions of the same name, so M3's campaign slug is identical to M2's and both modules' links roll into the same campaign in HubSpot/analytics — verified against a real M2 run: `pipeline-after-the-webinar-2026-08-19`. `with_utm()` generalizes M2's version (which hardcodes `utm_source=webinar`/`utm_medium=email`, M2's only channel) to accept a per-channel source/medium — same four `utm_*` keys, same campaign format, not a second scheme.

| Asset | Link tagged | `utm_source` | `utm_medium` | `utm_campaign` | `utm_content` |
|---|---|---|---|---|---|
| `blog.md` | closing CTA → recording | `blog` | `content` | `<event-name-slug>-<date>` | `blog` |
| `youtube.md` | inserted description link → recording | `youtube` | `video` | `<event-name-slug>-<date>` | `youtube-description` |
| `social.md`, LinkedIn posts | each post's `[link]` token → recording | `linkedin` | `social` | `<event-name-slug>-<date>` | `social-post-<N>` |
| `social.md`, X posts | each post's `[link]` token → recording | `x` | `social` | `<event-name-slug>-<date>` | `social-post-<N>` |

`social.md`'s `[link]` placeholder token (mandated by `prompts/social.md`'s output contract, present in both the offline sample and every live generation) is what gets replaced — one tagged link per post, so a closed deal three months out can be traced to the exact post, not just "something webinar-related."

## Transcription lane

The assignment's tool list for M3 is "Transcription API, LLM for generation, n8n for the pipeline, image generation for visual assets" — but `repurpose.py` only ever consumed a pre-written `transcript.md`. `transcribe.py` closes that gap: real audio in, real STT transcript out.

**Command** (against a real recording):

```bash
export SARVAM_API_KEY=<your key>   # https://api.sarvam.ai — never committed, never printed
python3 transcribe.py --audio your_recording.wav --out out/live-proof-transcription --compare source_segment.txt
```

`--dry-run` prints the request plan (chunk boundaries, endpoint, model, language, whether the API key env var is set) and makes **zero** network calls — use it to sanity-check before spending API credits. Input must be PCM WAV; convert anything else first: `ffmpeg -i in.mp3 -ar 16000 -ac 1 -c:a pcm_s16le out.wav`.

**Provider:** Sarvam AI, `saaras:v3`, sync `/speech-to-text` endpoint (`api-subscription-key` header, multipart body) — stdlib `urllib`, no dependencies. Audio over ~28s is chunked and sent as multiple real calls (Sarvam's sync endpoint hard-caps at 30s per request; there's a documented async batch endpoint for longer files, but it returned `Completed`/empty-transcript on every attempt in this session — see `out/live-proof-transcription/transcription_meta.json` → `known_provider_issue` for the job IDs). It fails loud (non-zero exit, message on stderr, nothing written) on a missing/empty API key, unreadable/non-WAV audio, or any network/HTTP error — it never falls back to a fixture transcript.

**What's real vs. stand-in in `out/live-proof-transcription/`:** there is no actual webinar recording, so the "recording" (`acmerevenue-2026-07-20_segment.wav`, 85.3s, two speakers) was synthesized from four turns of `data/incoming/transcript.md` using macOS `say` (Sarvam's own TTS tool — `bulbul:v3` — errored on every call in this session with a pitch/loudness parameter bug that reproduced even with those parameters omitted; documented in `transcription_meta.json` → `tts_provider_issue`). The transcription step itself is fully real: three genuine `saaras:v3` API calls (via the Sarvam MCP tool, which holds its own key — this session had no raw `SARVAM_API_KEY` in its shell env to run `transcribe.py`'s network path end-to-end, only `--dry-run` and the fail-loud path were exercised directly), with request IDs and latencies logged in `run_log.txt`. Output: 254 words, word-level similarity vs. the source segment 0.9216 (`difflib.SequenceMatcher`), both recorded in `transcription_meta.json`.

**Swapping in a real recording:** drop your own WAV (or convert with `ffmpeg` as above) and run the command above with `--compare` pointed at whatever ground-truth text you have for it, if any — `--compare` is optional and only adds the similarity score.

## Visual assets

The assignment's M3 tool list also names "image generation for visual assets," and `youtube.md`'s Thumbnail Brief / `infographic.md`'s Data Points + Layout were, until now, specs only — no pixels. `gen_visuals.py` closes that: it reads the live `youtube.md` and `infographic.md`, builds a grounded image-generation prompt from each (brief text, exact stats, hex colors — nothing invented), and produces a YouTube thumbnail (16:9) and an infographic hero image (portrait/square).

**Command:**

```bash
export OPENROUTER_API_KEY=<your key>   # or ~/.config/postevent/llm.env — never printed
python3 gen_visuals.py --youtube out/live-proof/m3/youtube.md \
    --infographic out/live-proof/m3/infographic.md --out out/m3-visuals
```

`--dry-run` writes `image_prompts.json` (the exact prompt payloads + the reviewer command) and makes zero network calls.

**This script is genuinely executable end to end, not a prompt-only stub.** It calls a real direct HTTP image API — OpenRouter's chat-completions endpoint with image output modality, model `google/gemini-2.5-flash-image` — using the same `OPENROUTER_API_KEY` already wired for this module's `--live` text path. The Higgsfield MCP tool named in the build brief for interactive sessions isn't callable from a plain script (MCP tools only exist inside a Claude session), so this is the fallback branch described in that brief, not the primary one; it was chosen because it verified working in this environment. It fails loud — HTTP error, `{"error":...}` payload, or a response with no image data all raise and exit 1 — no placeholder or SVG is ever substituted for a failed generation.

**Live proof:** `out/live-proof-visuals/` — `youtube-thumbnail-acmerevenue-2026-07-20.png` (1024×576, cropped from the model's native 1024×1024 output to true 16:9) and `infographic-hero-acmerevenue-2026-07-20.png` (1024×1024). `visuals_meta.json` records both the Higgsfield MCP attempt (0 credits on the free plan, no unlim allowance, `generate_image` call failed with the exact error `"Error starting generation: Requires basic plan or higher."`) and the OpenRouter generations actually used instead: model, full prompt sent, generation ID, dimensions, file size, and OpenRouter account usage (USD) before/after. It also logs known quality gaps found on inspection — the thumbnail's requested headline text didn't render legibly, and the infographic has three model-generated text typos (documented rather than silently shipped as clean).
