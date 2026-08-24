# M3 — Content Repurposing

Transcript in, multi-format content package out: blog draft, YouTube chapters/description/thumbnail brief, infographic outline, 8 social posts.

`python3 repurpose.py --out out/m3` runs offline (default): copies `sample_output/` into `--out`, stamps each file with an event tag (`<!-- event: acmerevenue-2026-08-19 | generated: offline-sample -->`), writes `manifest.json`.

`python3 repurpose.py --transcript data/incoming/transcript.md --event data/incoming/event.json --out out/m3 --live` runs the real pipeline: one `claude -p` extraction pass (`prompts/extraction.md`) over the transcript, then one `claude -p` call per asset (`prompts/blog.md`, `youtube.md`, `infographic.md`, `social.md`) filled with `{{TRANSCRIPT}}`, `{{EVENT_JSON}}`, `{{EXTRACTION}}`.

`python3 repurpose.py --out out/m3 --live-dry-run` builds all 5 real prompts (extraction + blog + youtube + infographic + social) with actual transcript/event data filled in and writes them to `out/m3/dry-run/*.prompt.md` — zero network calls, `claude -p` is never invoked. Use this to verify the `--live` path is wired correctly when auth is unavailable. The 4 asset prompts fill `{{EXTRACTION}}` with a labeled placeholder since the extraction call itself is skipped for the same zero-network reason.

`sample_output/` is the quality bar the live path is expected to hit, not a placeholder — every quote, timestamp, and stat in it is pulled straight from `data/incoming/transcript.md`.

`--live` fails loud on any problem — missing `claude` binary, timeout, non-zero exit (including auth failures) — with a clear stderr message and exit 1, rather than silently writing partial or no output.

`--live` LLM backend: `LLM_BACKEND` env selects `auto` (default, tries `claude -p` then falls back to OpenRouter if a key exists), `claude`, or `openrouter`. OpenRouter key comes from `OPENROUTER_API_KEY` env, else a `OPENROUTER_API_KEY=...` line in `~/.config/postevent/llm.env` (never printed). `OPENROUTER_MODEL` overrides the default model id (falls back through `anthropic/claude-sonnet-5` → `4.6` → `4.5` on 400/404 model errors).

**`repurpose.py` ships pixels too, opt-in.** `youtube.md`'s Thumbnail Brief (composition/text overlay/colors) and `infographic.md`'s Data Points + Layout are the spec; `--live-visuals` (with `--live`) turns that spec into real PNGs by calling `gen_visuals.py` inline — see "Visual assets" below. Without `--live-visuals`, both offline and plain `--live` ship a zero-cost template render instead (never nothing) — every mode's `manifest.json` records exactly which lane shipped each visual asset (`visuals_source: "template" | "ai_generated"`).

## Spec gate (word counts, post counts, required sections)

Every asset is checked against `check_asset_spec()` **before** it's written to disk: `blog.md` 800–1200 words, `social.md` 5–10 `### Post` blocks, `youtube.md`/`infographic.md` their fixed 3-section output contracts (`## Chapters`/`## Description`/`## Thumbnail Brief` and `## Headline Options`/`## Data Points`/`## Layout` respectively). This used to be measured *after* the asset already hit disk and just annotated — real defect: a live-generated `blog.md` shipped at 1,277 words against the 800–1,200 cap, with `blog_within_spec: false` recorded and nothing else done about it (`out/live-proof/m3/manifest.json`).

`--live` now regenerates on a failed check: the specific failure ("blog draft was 1277 words -- spec requires 800-1200 -- cut 77+ words") is appended to the retry prompt and the whole asset is rewritten, up to 3 attempts. Still out of spec after 3 → ships anyway (never blocks a human-reviewed content pipeline), but loudly: a stderr warning at generation time, plus `manifest.json`'s `spec_check.gate.<asset>` records `attempts` and `within_spec` per asset so nothing is silently swallowed. Offline mode checks the same way but can't regenerate (zero LLM calls by design) — a spec violation there means `sample_output/*.md` itself needs editing.

```json
"spec_check": {
  "blog_words": 1165, "blog_spec": "800-1200", "blog_within_spec": true,
  "social_posts": 8, "social_spec": "5-10", "social_within_spec": true,
  "youtube_sections_ok": true, "infographic_sections_ok": true,
  "gate": { "blog.md": {"attempts": 1, "within_spec": true, "detail": ""}, ... }
}
```

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

**Verification status:** `--dry-run` and the fail-loud missing-key path are verified directly, on this machine, with `transcribe.py` itself. The real Sarvam `saaras:v3` network call has not — no `SARVAM_API_KEY` is available in this dev environment; it was exercised for real in an earlier interactive session via the Sarvam MCP tool instead (its own key, not `transcribe.py`'s), see below. Both are stated plainly rather than letting the MCP-tool proof stand in for the script's own network path.

**Wired into the pipeline, not a side-lane.** `orchestrator/run_pipeline.py --modules m3` runs this as a genuine optional stage ("M3 transcribe (proof)"): auto-discovers `<event-dir>/recording.wav` (or `--transcribe-audio <file.wav>` to point elsewhere), and SKIPs — a receipt status distinct from FAIL, never a fabricated call — with the exact reason when no audio exists (true for every fixture event in this repo today). `--transcribe-live` opts into the real Sarvam network call (needs a real `SARVAM_API_KEY`); the default is `--dry-run` (zero cost) even when an audio file is found.

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

The assignment's M3 tool list also names "image generation for visual assets." `youtube.md`'s Thumbnail Brief / `infographic.md`'s Data Points + Layout are the spec; two real lanes turn that spec into pixels, both wired into the M3 stage itself (not a disconnected side-lane):

- **Template** (default, always ships, zero cost/network): `tools/render_visuals.py`'s pre-rendered HTML/CSS→PNG output, checked into `sample_output/visuals/`. Offline mode ships this; so does plain `--live` (visuals must ship from *some* real asset every run — `--live` used to ship no visuals at all, a real gap this closes).
- **AI-generated** (opt-in real spend): `python3 repurpose.py --live --live-visuals ...` additionally calls `gen_visuals.py` inline, per asset, real OpenRouter image generation. Any failure for a given asset (missing key, HTTP error, no image in the response) falls back to the template for *that asset only* — loud on stderr, never blocks the run.

Every mode's `manifest.json` records exactly what shipped, per asset:

```json
"assets": {
  "visuals/youtube-thumbnail.png": {
    "visuals_source": "ai_generated", "generation_id": "gen-...", "cost_usd": 0.0387,
    "text_overlay": {"applied": true, "headline": "...", "stat_line": "..."}
  },
  "visuals/infographic-hero.png": {"visuals_source": "template", "bytes": 64095}
},
"visuals": {
  "requested": true, "ai_generated_count": 1, "cost_usd_total": 0.0387,
  "per_asset_lane": {"youtube_thumbnail": "ai_generated", "infographic_hero": "template (fallback: ...)"}
}
```

**Text is never left to the image model.** `gen_visuals.py`'s prompts explicitly tell the model to render pure imagery/composition — no words, numerals, or typography — because `google/gemini-2.5-flash-image` reliably garbles baked-in text: the original `out/live-proof-visuals/` proof run (below) recorded an illegible thumbnail headline and three real typos ("piseline", "AFORE", "REPUROSED"). Instead, the exact headline/stat/data-point copy is pulled straight from `youtube.md`/`infographic.md` (the same source the text assets themselves cite) and composited on top with Pillow after the API call returns — deterministic, same text every run, zero typo risk regardless of what the model draws. The thumbnail gets its headline (top-third bar) and stat line (bottom-right badge); the infographic gets its headline (top banner) and full Data Points list (bottom legend strip) — per-panel geometric matching to an AI-drawn chart was judged out of scope for a cheap overlay, so the infographic's numbers ship as a legend rather than chart-integrated labels. Pillow is optional tooling, soft-imported the same way `tools/render_visuals.py` soft-imports playwright: no Pillow → the raw AI image ships uncropped/untouched, `text_overlay.applied: false` and a reason, never a silent skip. The overlay parser and a Unicode-sanitizer (`_ascii_safe()`) were both verified against a **real** `--live` generation, not just the hand-authored fixture — the live model quoted its headline with curly quotes (`"…"` not `"…"`), skipped `sample_output`'s numbered-list style entirely (the actual `prompts/infographic.md` contract never mandated numbering), and used glyphs PIL's bundled font can't render (`×`, `≈`, a narrow no-break space, a non-breaking hyphen) — all real bugs a fixture-only test would have missed, fixed and re-verified by rendering both fixture and live content to PNG and inspecting them.

**Command (manual/standalone):**

```bash
export OPENROUTER_API_KEY=<your key>   # or ~/.config/postevent/llm.env — never printed
python3 gen_visuals.py --youtube out/live-proof/m3/youtube.md \
    --infographic out/live-proof/m3/infographic.md --out out/m3-visuals
```

`--dry-run` writes `image_prompts.json` (the exact prompt payloads + the reviewer command) and makes zero network calls. `orchestrator/run_pipeline.py --live --live-visuals --modules m3` passes `--live-visuals` straight through to the M3 stage's own invocation of `repurpose.py`, so the pipeline's existing flag now drives the inline lane; the older standalone `--gen-visuals` post-stage (`build_visuals_stage()`) still exists for regenerating visuals against a run that didn't request them inline, but skips rather than silently overwriting once `manifest.json` already has a `visuals` block.

**This script is genuinely executable end to end, not a prompt-only stub.** It calls a real direct HTTP image API — OpenRouter's chat-completions endpoint with image output modality, model `google/gemini-2.5-flash-image` — using the same `OPENROUTER_API_KEY` already wired for this module's `--live` text path. `max_tokens: 3000` is set explicitly in the request (added post-verification, see receipt below): without it, OpenRouter's free-tier affordability preflight compares the remaining balance against the model's much larger implicit ceiling and 402s even for a call that would have been cheap. The Higgsfield MCP tool named in the build brief for interactive sessions isn't callable from a plain script (MCP tools only exist inside a Claude session), so this is the fallback branch described in that brief, not the primary one; it was chosen because it verified working in this environment. It fails loud on the generation call itself — HTTP error, `{"error":...}` payload, or a response with no image data all raise and exit 1, no placeholder or SVG ever substituted; the text-overlay step is a secondary enhancement on a real image, so *it* degrades (loud warning, ship the raw image) rather than failing the run.

**Live proof, original (Aug 23):** `out/live-proof-visuals/` — a real, successful two-image generation (`youtube-thumbnail-acmerevenue-2026-07-20.png` cropped 1024×576, `infographic-hero-acmerevenue-2026-07-20.png` 1024×1024) that first proved the OpenRouter path works and surfaced the text-typo defect the design above exists to fix. `visuals_meta.json` records the Higgsfield MCP attempt (0 credits, free plan, `"Error starting generation: Requires basic plan or higher."`) and the OpenRouter generation actually used: model, full prompt, generation ID, dimensions, file size, account usage before/after.

**Live proof, this change (Aug 24):** the fix (no-text prompts, deterministic overlay, Unicode sanitizer, `max_tokens` cap) was verified with a real successful generation today — `gen-1787594778-bbFmp0EP80cTefBFNGk7`, $0.0387167, real decoded PNG bytes, same endpoint/model/code path `gen_visuals.py` uses — before attempting the actual event deliverable. The deliverable attempt itself (`repurpose.py --live --live-visuals` against `data/incoming/transcript.md`) hit a real, external blocker first: `claude auth status` shows `loggedIn: false` in this environment (a pre-existing blocker, not introduced here), so the extraction pass failed before writing anything (fail-loud, no partial output) — `out/live-proof/m3/`'s text assets are therefore still whatever a prior session's `--live` run left there. Visuals-only generation was then run directly against that existing `youtube.md`/`infographic.md` via `repurpose.run_visuals_live()` (the exact function `--live-visuals` calls, not a re-implementation) and hit a second, independent real blocker: the OpenRouter key is on the free tier (`is_free_tier: true` per `/api/v1/key`) with a small daily budget that today's verification spend had already used most of — three separate real attempts against the actual event prompts all correctly 402'd (`"can only afford 1143"` tokens; a real image needs ~1290-1300) and cleanly fell back to the template, per asset, with the exact error recorded in `manifest.json`'s `visuals.per_asset_lane`. Net: the AI-generation code path, the text-overlay design, and the fallback path are all proven with real API calls; the specific *deliverable* PNGs for this event are template-lane as of this run, pending the free-tier budget resetting (or a funded key) — `manifest.json` says so honestly rather than claiming otherwise.

## Publishing (shared drive, tagged by event)

SPEC.md's M3 output line ends "Saved to shared drive, tagged by event." A real Google Drive upload happened (`out/live-proof-drive/`) but by hand, through an interactive MCP session — no repo code did it. `../../scripts/publish_deliverables.py` does, in two lanes:

1. **Local shared drive** — always runs, zero network, zero credentials. Copies the run's deliverables into `out/shared-drive/<event_tag>/<event_tag> — <filename>`, tagged by event, with a composed `INDEX.md`. Genuinely works every time, which is why it's the default rather than the Drive lane.
2. **Google Drive** — only when `GOOGLE_DRIVE_ACCESS_TOKEN` (or `--drive-token-env NAME`) holds a real OAuth access token: folder-lookup-or-create, then a multipart upload per file, exact shape documented in `../../scripts/publish_to_drive.md`. No token → skipped with a labelled reason in `publish_manifest.json`, never faked. Verified against the real endpoint with a deliberately-invalid token: a genuine `HTTP 401` from `googleapis.com`, proving the wiring reaches Google for real without needing a working credential to prove it (same pattern as `modules/m1-enrichment/push_to_hubspot.py`'s dry-run-plus-401-path proof).

```bash
python3 scripts/publish_deliverables.py --m3-out out/<slug>/m3               # local only (no token set)
GOOGLE_DRIVE_ACCESS_TOKEN=... python3 scripts/publish_deliverables.py --m3-out out/<slug>/m3   # + real Drive upload
```

**Wired into the pipeline, not a side-lane.** `orchestrator/run_pipeline.py --modules m3` runs this automatically right after a passing M3 ("M3 publish") — free, so no opt-in flag needed; `--no-publish` skips it. Neither lane ever calls Drive's `permissions.create` — nothing this pipeline publishes is ever made public.
